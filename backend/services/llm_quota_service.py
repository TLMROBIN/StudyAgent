from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.models.llm_model import LLMModelConfig, LLMQuotaPolicy, QuotaBillingMode
from backend.models.llm_usage import LLMUsageEvent
from backend.models.user import User
from backend.services.metrics_service import llm_quota_denied_total, llm_quota_reserved_total, llm_usage_recorded_total
from backend.services.store_service import BaseStore, QuotaCounterKey, store


@dataclass
class QuotaReservation:
    allowed: Literal[True]
    reservation_key: str
    model_config: LLMModelConfig
    policy: LLMQuotaPolicy
    billing_mode: QuotaBillingMode
    reserved_amount: int
    estimated: bool
    user_id: int
    request_id: str | None


@dataclass
class QuotaDenied:
    allowed: Literal[False]
    code: str
    message: str
    model_key: str
    billing_mode: str
    reason: str
    reset_hint: str = "明天 00:00 后恢复"


@dataclass
class QuotaSnapshot:
    daily_request_limit: int | None
    remaining_requests: int | None
    daily_token_limit: int | None
    remaining_tokens: int | None
    quota_exhausted: bool
    message: str


class LLMQuotaService:
    def __init__(self, quota_store: BaseStore | None = None, *, store: BaseStore | None = None) -> None:
        self.store = quota_store or store or globals()["store"]

    def check_and_reserve(
        self,
        *,
        db: Session,
        user: User,
        model_config: LLMModelConfig,
        request_id: str | None,
        prompt_messages: list[dict[str, Any]],
    ) -> QuotaReservation | QuotaDenied:
        policy = model_config.quota_policy or self._free_policy(model_config)
        billing_mode = policy.billing_mode
        reservation_key = f"quota:reservation:{request_id or uuid4().hex}"

        if billing_mode == QuotaBillingMode.FREE_LOCAL:
            return QuotaReservation(
                allowed=True,
                reservation_key=reservation_key,
                model_config=model_config,
                policy=policy,
                billing_mode=billing_mode,
                reserved_amount=0,
                estimated=False,
                user_id=user.id,
                request_id=request_id,
            )

        keys, amount, ttl_seconds = self._reservation_keys_and_amount(user, model_config, policy, prompt_messages)
        try:
            result = self.store.reserve_quota(keys, reservation_key, amount=amount, ttl_seconds=ttl_seconds)
        except Exception:
            if policy.fail_closed_on_store_error:
                return self._deny(model_config, policy, "store_unavailable", "额度服务暂时不可用，请稍后再试。")
            return QuotaReservation(
                allowed=True,
                reservation_key=reservation_key,
                model_config=model_config,
                policy=policy,
                billing_mode=billing_mode,
                reserved_amount=amount,
                estimated=billing_mode == QuotaBillingMode.TOKEN_USAGE,
                user_id=user.id,
                request_id=request_id,
            )

        if not result.allowed:
            reason = self._denial_reason(result.exceeded_key or "")
            llm_quota_denied_total.labels(
                model_key=model_config.model_key,
                billing_mode=billing_mode.value,
                reason=reason,
            ).inc()
            return self._deny(model_config, policy, reason, "该模型今日额度已用完，请明天再试或切换其他模型。")

        llm_quota_reserved_total.labels(model_key=model_config.model_key, billing_mode=billing_mode.value).inc()
        return QuotaReservation(
            allowed=True,
            reservation_key=reservation_key,
            model_config=model_config,
            policy=policy,
            billing_mode=billing_mode,
            reserved_amount=result.amount,
            estimated=billing_mode == QuotaBillingMode.TOKEN_USAGE,
            user_id=user.id,
            request_id=request_id,
        )

    def release(self, reservation: QuotaReservation) -> None:
        self.store.release_quota(reservation.reservation_key)

    def reconcile(
        self,
        *,
        db: Session,
        reservation: QuotaReservation,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        reasoning_tokens: int = 0,
        prompt_cache_hit_tokens: int = 0,
        prompt_cache_miss_tokens: int = 0,
        request_count: int | None = None,
        source: str,
        estimated: bool,
        user_id: int | None = None,
        conversation_id: int | None = None,
        message_id: int | None = None,
        request_id: str | None = None,
    ) -> LLMUsageEvent:
        if reservation.billing_mode == QuotaBillingMode.TOKEN_USAGE:
            actual_total = max(0, total_tokens or prompt_tokens + completion_tokens or reservation.reserved_amount)
            self.store.reconcile_quota(reservation.reservation_key, actual_total)
        event = LLMUsageEvent(
            user_id=user_id or reservation.user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            request_id=request_id or reservation.request_id,
            model_config_id=reservation.model_config.id,
            provider_account_id=reservation.model_config.provider_account_id,
            model_key=reservation.model_config.model_key,
            provider_name=reservation.model_config.provider_account.provider_name,
            provider_model=reservation.model_config.provider_model,
            billing_mode=reservation.billing_mode.value,
            actual_model_key=reservation.model_config.model_key,
            actual_provider_model=reservation.model_config.provider_model,
            request_count=request_count if request_count is not None else (1 if reservation.billing_mode == QuotaBillingMode.REQUEST_COUNT else 0),
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            total_tokens=max(0, total_tokens),
            reasoning_tokens=max(0, reasoning_tokens),
            prompt_cache_hit_tokens=max(0, prompt_cache_hit_tokens),
            prompt_cache_miss_tokens=max(0, prompt_cache_miss_tokens),
            estimated=estimated,
            source=source,
            policy_snapshot_json=self._policy_snapshot(reservation.policy),
            reservation_key=reservation.reservation_key,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        llm_usage_recorded_total.labels(
            model_key=reservation.model_config.model_key,
            billing_mode=reservation.billing_mode.value,
            source=source,
        ).inc()
        return event

    def record_usage_event(
        self,
        *,
        db: Session,
        reservation: QuotaReservation,
        source: str,
        user_id: int,
        conversation_id: int | None = None,
        message_id: int | None = None,
        request_id: str | None = None,
    ) -> LLMUsageEvent:
        return self.reconcile(
            db=db,
            reservation=reservation,
            source=source,
            estimated=reservation.estimated,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            request_id=request_id,
        )

    def quota_snapshot_for_user(self, *, db: Session, user: User, model_config: LLMModelConfig) -> QuotaSnapshot:
        policy = model_config.quota_policy or self._free_policy(model_config)
        if policy.billing_mode == QuotaBillingMode.REQUEST_COUNT:
            limit = policy.user_daily_request_limit
            remaining = None
            if limit:
                snapshot = self.store.quota_snapshot([QuotaCounterKey(self._user_request_day_key(user.id, model_config.model_key), limit)])[0]
                remaining = snapshot.remaining
            exhausted = remaining == 0 if remaining is not None else False
            return QuotaSnapshot(
                daily_request_limit=limit,
                remaining_requests=remaining,
                daily_token_limit=None,
                remaining_tokens=None,
                quota_exhausted=exhausted,
                message=f"今日剩余 {remaining} 次" if remaining is not None else "不限次数",
            )
        if policy.billing_mode == QuotaBillingMode.TOKEN_USAGE:
            limit = policy.user_daily_token_limit
            remaining = None
            if limit:
                snapshot = self.store.quota_snapshot([QuotaCounterKey(self._user_token_day_key(user.id, model_config.model_key), limit)])[0]
                remaining = snapshot.remaining
            exhausted = remaining == 0 if remaining is not None else False
            return QuotaSnapshot(
                daily_request_limit=None,
                remaining_requests=None,
                daily_token_limit=limit,
                remaining_tokens=remaining,
                quota_exhausted=exhausted,
                message=f"今日剩余 {remaining} tokens" if remaining is not None else "不限 tokens",
            )
        return QuotaSnapshot(None, None, None, None, False, "本地模型不计外部额度")

    def _reservation_keys_and_amount(
        self,
        user: User,
        model_config: LLMModelConfig,
        policy: LLMQuotaPolicy,
        prompt_messages: list[dict[str, Any]],
    ) -> tuple[list[QuotaCounterKey], int, int]:
        if policy.billing_mode == QuotaBillingMode.REQUEST_COUNT:
            keys: list[QuotaCounterKey] = []
            if policy.user_daily_request_limit:
                keys.append(QuotaCounterKey(self._user_request_day_key(user.id, model_config.model_key), policy.user_daily_request_limit))
            if policy.school_daily_request_limit:
                keys.append(QuotaCounterKey(self._school_request_day_key(model_config.model_key), policy.school_daily_request_limit))
            if policy.provider_rolling_5h_request_limit:
                keys.append(
                    QuotaCounterKey(
                        f"quota:req:provider:{model_config.provider_account_id}:model:{model_config.model_key}:rolling5h",
                        policy.provider_rolling_5h_request_limit,
                    )
                )
            if policy.provider_weekly_request_limit:
                iso = datetime.now(UTC).isocalendar()
                keys.append(
                    QuotaCounterKey(
                        f"quota:req:provider:{model_config.provider_account_id}:model:{model_config.model_key}:week:{iso.year}-W{iso.week}",
                        policy.provider_weekly_request_limit,
                    )
                )
            return keys, 1, 7 * 24 * 3600

        estimate = self._estimate_prompt_tokens(prompt_messages) + int(policy.max_completion_tokens or 0)
        keys = []
        if policy.user_daily_token_limit:
            keys.append(QuotaCounterKey(self._user_token_day_key(user.id, model_config.model_key), policy.user_daily_token_limit))
        if policy.school_daily_token_limit:
            keys.append(QuotaCounterKey(self._school_token_day_key(model_config.model_key), policy.school_daily_token_limit))
        return keys, max(1, estimate), 24 * 3600

    @staticmethod
    def _estimate_prompt_tokens(prompt_messages: list[dict[str, Any]]) -> int:
        text = ""
        for message in prompt_messages:
            content = message.get("content")
            if isinstance(content, list):
                text += " ".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
            else:
                text += str(content or "")
        chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        other_chars = max(0, len(text) - chinese_chars)
        return max(1, chinese_chars + (other_chars + 3) // 4)

    @staticmethod
    def _free_policy(model_config: LLMModelConfig) -> LLMQuotaPolicy:
        return LLMQuotaPolicy(model_config_id=model_config.id, billing_mode=QuotaBillingMode.FREE_LOCAL)

    @staticmethod
    def _policy_snapshot(policy: LLMQuotaPolicy) -> dict[str, Any]:
        return {
            "billing_mode": policy.billing_mode.value,
            "user_daily_request_limit": policy.user_daily_request_limit,
            "user_daily_token_limit": policy.user_daily_token_limit,
            "school_daily_request_limit": policy.school_daily_request_limit,
            "school_daily_token_limit": policy.school_daily_token_limit,
            "provider_rolling_5h_request_limit": policy.provider_rolling_5h_request_limit,
            "provider_weekly_request_limit": policy.provider_weekly_request_limit,
            "max_completion_tokens": policy.max_completion_tokens,
            "count_cache_hit": policy.count_cache_hit,
        }

    @staticmethod
    def _deny(model_config: LLMModelConfig, policy: LLMQuotaPolicy, reason: str, message: str) -> QuotaDenied:
        return QuotaDenied(
            allowed=False,
            code="llm_quota_exhausted" if reason != "store_unavailable" else "llm_quota_unavailable",
            message=message,
            model_key=model_config.model_key,
            billing_mode=policy.billing_mode.value,
            reason=reason,
        )

    @staticmethod
    def _denial_reason(exceeded_key: str) -> str:
        if ":user:" in exceeded_key:
            return "user_daily_limit"
        if ":school:" in exceeded_key:
            return "school_daily_limit"
        if ":rolling5h" in exceeded_key:
            return "provider_rolling_5h_limit"
        if ":week:" in exceeded_key:
            return "provider_weekly_limit"
        return "quota_limit"

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _user_request_day_key(self, user_id: int, model_key: str) -> str:
        return f"quota:req:user:{user_id}:model:{model_key}:day:{self._today()}"

    def _school_request_day_key(self, model_key: str) -> str:
        return f"quota:req:school:model:{model_key}:day:{self._today()}"

    def _user_token_day_key(self, user_id: int, model_key: str) -> str:
        return f"quota:tok:user:{user_id}:model:{model_key}:day:{self._today()}"

    def _school_token_day_key(self, model_key: str) -> str:
        return f"quota:tok:school:model:{model_key}:day:{self._today()}"


llm_quota_service = LLMQuotaService()
