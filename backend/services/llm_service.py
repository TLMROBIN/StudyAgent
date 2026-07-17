from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from base64 import b64encode
import json
import logging
import re
from time import monotonic
from typing import AsyncIterator, Literal

import httpx

from backend.config import get_settings
from backend.database import SessionLocal
from backend.services.metrics_service import (
    chat_image_vision_call_failures_total,
    llm_stream_fallback_total,
    llm_stream_provider_failure_total,
)

logger = logging.getLogger(__name__)

OPEN_THINK_TAG = "<think>"
CLOSE_THINK_TAG = "</think>"
NO_IMAGE_RECEIVED_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"(?:没有|未|没)收到.*图片",
        r"(?:没有|未|没)看到.*图片",
        r"看不到.*图片",
        r"无法(?:查看|识别|读取|看清).*图片",
        r"不能(?:看到|查看|识别|读取).*图片",
        r"请(?:重新)?上传.*图片",
        r"图片.*(?:未|没有|没)提供",
    ]
]


def _partial_tag_suffix_length(text: str, tag: str) -> int:
    max_size = min(len(text), len(tag) - 1)
    for size in range(max_size, 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


def _content_text(content: object) -> str:
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    return str(content or "")


class ThinkingContentFilter:
    def __init__(self) -> None:
        self.buffer = ""
        self.inside_think = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self.buffer += text
        visible_parts: list[str] = []

        while self.buffer:
            if self.inside_think:
                close_index = self.buffer.find(CLOSE_THINK_TAG)
                if close_index == -1:
                    keep = _partial_tag_suffix_length(self.buffer, CLOSE_THINK_TAG)
                    self.buffer = self.buffer[-keep:] if keep else ""
                    break
                self.buffer = self.buffer[close_index + len(CLOSE_THINK_TAG) :]
                self.inside_think = False
                continue

            open_index = self.buffer.find(OPEN_THINK_TAG)
            if open_index != -1:
                if open_index > 0:
                    visible_parts.append(self.buffer[:open_index])
                self.buffer = self.buffer[open_index + len(OPEN_THINK_TAG) :]
                self.inside_think = True
                continue

            keep = max(
                _partial_tag_suffix_length(self.buffer, OPEN_THINK_TAG),
                _partial_tag_suffix_length(self.buffer, CLOSE_THINK_TAG),
            )
            emit_upto = len(self.buffer) - keep
            if emit_upto > 0:
                visible_parts.append(self.buffer[:emit_upto])
                self.buffer = self.buffer[emit_upto:]
            else:
                break

        return "".join(visible_parts)

    def flush(self) -> str:
        if self.inside_think:
            self.buffer = ""
            return ""

        keep = max(
            _partial_tag_suffix_length(self.buffer, OPEN_THINK_TAG),
            _partial_tag_suffix_length(self.buffer, CLOSE_THINK_TAG),
        )
        if keep:
            self.buffer = self.buffer[:-keep]

        output = self.buffer
        self.buffer = ""
        return output


class LeadingModeTagParser:
    """Parse and strip a leading <mode>fact|guide</mode> tag from a stream."""

    MAX_BUFFER_CHARS = 32
    FACT_TAG = "<mode>fact</mode>"
    GUIDE_TAG = "<mode>guide</mode>"
    TAG_RE = re.compile(r"^\s*<mode>(fact|guide)</mode>")

    def __init__(self) -> None:
        self.buffer = ""
        self.resolved_mode: str | None = None

    def feed(self, text: str) -> tuple[str | None, str]:
        if not text:
            return None, ""
        if self.resolved_mode is not None:
            return None, text

        self.buffer += text
        match = self.TAG_RE.match(self.buffer)
        if match:
            self.resolved_mode = match.group(1)
            visible = self.buffer[match.end() :]
            self.buffer = ""
            return self.resolved_mode, visible

        stripped = self.buffer.lstrip()
        if len(self.buffer) > self.MAX_BUFFER_CHARS:
            return self._resolve_without_tag()
        if not stripped or self.FACT_TAG.startswith(stripped) or self.GUIDE_TAG.startswith(stripped):
            return None, ""
        return self._resolve_without_tag()

    def flush(self) -> str:
        if self.resolved_mode is not None:
            output = self.buffer
            self.buffer = ""
            return output
        self.resolved_mode = "guide"
        output = self.buffer
        self.buffer = ""
        return output

    def _resolve_without_tag(self) -> tuple[str, str]:
        self.resolved_mode = "guide"
        visible = self.buffer
        self.buffer = ""
        return self.resolved_mode, visible


@dataclass
class ProviderState:
    name: str
    base_url: str | None
    api_key: str | None
    model: str
    failures: int = 0
    open_until: datetime | None = None

    @property
    def available(self) -> bool:
        return self.open_until is None or self.open_until <= datetime.now(UTC)


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


@dataclass
class LLMStreamEvent:
    type: Literal["chunk", "usage"]
    content: str = ""
    usage: LLMUsage | None = None
    provider_name: str = ""
    provider_model: str = ""


class LLMService:
    DEFAULT_CHAT_MODEL_KEY = "deepseek-v4-flash"

    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self._session_factory = SessionLocal
        self.providers = [
            ProviderState(
                name=settings.llm_primary_name,
                base_url=settings.llm_primary_base_url,
                api_key=settings.llm_primary_api_key,
                model=settings.llm_primary_model,
            ),
            ProviderState(
                name=settings.llm_fallback_name,
                base_url=settings.llm_fallback_base_url,
                api_key=settings.llm_fallback_api_key,
                model=settings.llm_fallback_model,
            ),
        ]
        self._model_status_cache: dict[str, tuple[datetime, dict[str, str]]] = {}
        self._model_status_ttl_seconds = 300
        self._chat_http_client: httpx.AsyncClient | None = None
        self._vision_http_client: httpx.AsyncClient | None = None

    async def _get_chat_http_client(self) -> httpx.AsyncClient:
        if self._chat_http_client is None or getattr(self._chat_http_client, "is_closed", False):
            timeout = httpx.Timeout(float(self.settings.llm_request_timeout_seconds))
            self._chat_http_client = httpx.AsyncClient(timeout=timeout)
        return self._chat_http_client

    async def _get_vision_http_client(self) -> httpx.AsyncClient:
        if self._vision_http_client is None or getattr(self._vision_http_client, "is_closed", False):
            timeout = httpx.Timeout(float(self.settings.effective_chat_image_vision_timeout_seconds))
            self._vision_http_client = httpx.AsyncClient(timeout=timeout)
        return self._vision_http_client

    async def aclose(self) -> None:
        clients = (self._chat_http_client, self._vision_http_client)
        self._chat_http_client = None
        self._vision_http_client = None
        for client in clients:
            if client is not None and not getattr(client, "is_closed", False):
                await client.aclose()

    def chat_model_options(self) -> list[dict[str, str]]:
        configured = self._database_chat_model_options()
        if configured:
            return configured
        return [
            {
                "key": self.DEFAULT_CHAT_MODEL_KEY,
                "name": "DeepSeek V4 Flash",
                "description": "通用快捷",
            },
        ]

    def normalize_chat_model_key(self, model_key: str | None) -> str:
        candidate = (model_key or self.DEFAULT_CHAT_MODEL_KEY).strip()
        allowed = {item["key"] for item in self.chat_model_options()}
        if candidate not in allowed:
            raise ValueError(f"Unsupported chat model: {candidate}")
        return candidate

    def _runtime_providers(self) -> list[ProviderState]:
        configured = self._database_providers()
        return configured or self.providers

    def _providers_for_chat_model(self, model_key: str | None) -> list[ProviderState]:
        selected_key = self.normalize_chat_model_key(model_key)
        configured = self._database_providers_for_model(selected_key)
        if configured:
            return configured
        return self._runtime_providers()

    async def chat_model_statuses(self, *, force_refresh: bool = False) -> list[dict[str, str]]:
        statuses: list[dict[str, str]] = []
        for option in self.chat_model_options():
            key = option["key"]
            cached = self._model_status_cache.get(key)
            if cached and not force_refresh:
                checked_at, status = cached
                if (datetime.now(UTC) - checked_at).total_seconds() < self._model_status_ttl_seconds:
                    statuses.append(status)
                    continue

            status = (
                await self._check_chat_model_status(key)
                if force_refresh
                else self._configured_chat_model_status(key)
            )
            self._model_status_cache[key] = (datetime.now(UTC), status)
            statuses.append(status)
        return statuses

    def _configured_chat_model_status(self, model_key: str) -> dict[str, str]:
        providers = self._providers_for_chat_model(model_key)
        if not providers:
            return {"key": model_key, "status": "unavailable", "message": "模型未配置"}

        has_configured_provider = False
        for provider in providers:
            if not provider.base_url or not provider.api_key:
                continue
            has_configured_provider = True
            if provider.available:
                return {"key": model_key, "status": "available", "message": ""}

        if has_configured_provider:
            return {"key": model_key, "status": "unavailable", "message": "模型服务暂时熔断"}
        return {"key": model_key, "status": "unavailable", "message": "模型未配置"}

    async def _check_chat_model_status(self, model_key: str) -> dict[str, str]:
        providers = self._providers_for_chat_model(model_key)
        if not providers:
            return {"key": model_key, "status": "unavailable", "message": "模型未配置"}

        failure_message = ""
        for provider in providers:
            ok, message = await self._probe_openai_compatible(provider)
            if ok:
                return {"key": model_key, "status": "available", "message": ""}
            failure_message = failure_message or message
        return {"key": model_key, "status": "unavailable", "message": failure_message or "模型不可用"}

    async def _probe_openai_compatible(self, provider: ProviderState) -> tuple[bool, str]:
        if not provider.base_url or not provider.api_key:
            return False, "模型未配置"
        if not provider.available:
            return False, "模型服务暂时熔断"

        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": provider.model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "stream": False,
            "max_completion_tokens": 8,
        }
        url = self._chat_completions_url(provider.base_url)
        try:
            client = await self._get_chat_http_client()
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return True, ""
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                return False, "认证失败"
            if status_code in {402, 429}:
                return False, "上游模型额度不足或请求被上游限流，请联系管理员检查供应商账户额度，或切换其他模型。"
            if status_code >= 500:
                return False, "模型服务异常"
            return False, f"模型服务返回 {status_code}"
        except httpx.TimeoutException:
            return False, "模型探测超时"
        except httpx.HTTPError:
            return False, "连接失败或服务未运行"

    def _database_providers(self) -> list[ProviderState]:
        try:
            from sqlalchemy import select

            from backend.models.llm_provider import LLMProviderConfig

            session = self._session_factory()
            try:
                items = session.scalars(
                    select(LLMProviderConfig)
                    .where((LLMProviderConfig.is_active.is_(True)) | (LLMProviderConfig.is_fallback.is_(True)))
                    .order_by(LLMProviderConfig.is_active.desc(), LLMProviderConfig.id.asc())
                ).all()
                return [
                    ProviderState(
                        name=item.name,
                        base_url=item.base_url,
                        api_key=item.api_key,
                        model=item.model,
                    )
                    for item in items
                ]
            finally:
                session.close()
        except Exception:
            return []

    def _database_chat_model_options(self) -> list[dict[str, str]]:
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from backend.models.llm_model import LLMModelConfig

            session = self._session_factory()
            try:
                rows = session.scalars(
                    select(LLMModelConfig)
                    .options(selectinload(LLMModelConfig.quota_policy))
                    .where(LLMModelConfig.is_enabled.is_(True), LLMModelConfig.capability_text.is_(True))
                    .order_by(LLMModelConfig.sort_order.asc(), LLMModelConfig.id.asc())
                ).all()
                return [
                    {
                        "key": item.model_key,
                        "name": item.display_name,
                        "description": item.description,
                    }
                    for item in rows
                ]
            finally:
                session.close()
        except Exception:
            return []

    def _database_providers_for_model(self, model_key: str) -> list[ProviderState]:
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from backend.models.llm_model import LLMModelConfig

            session = self._session_factory()
            try:
                selected = session.scalar(
                    select(LLMModelConfig)
                    .options(selectinload(LLMModelConfig.provider_account))
                    .where(
                        LLMModelConfig.model_key == model_key,
                        LLMModelConfig.is_enabled.is_(True),
                        LLMModelConfig.capability_text.is_(True),
                    )
                )
                if not selected or not selected.provider_account or not selected.provider_account.is_enabled:
                    return []
                fallback_rows = session.scalars(
                    select(LLMModelConfig)
                    .options(selectinload(LLMModelConfig.provider_account))
                    .where(
                        LLMModelConfig.id != selected.id,
                        LLMModelConfig.is_enabled.is_(True),
                        LLMModelConfig.capability_text.is_(True),
                        LLMModelConfig.is_fallback.is_(True),
                    )
                    .order_by(LLMModelConfig.sort_order.asc(), LLMModelConfig.id.asc())
                ).all()
                providers: list[ProviderState] = []
                for item in [selected, *fallback_rows]:
                    account = item.provider_account
                    if not account or not account.is_enabled:
                        continue
                    providers.append(
                        ProviderState(
                            name=account.provider_name,
                            base_url=account.base_url,
                            api_key=account.api_key,
                            model=item.provider_model,
                        )
                    )
                return providers
            finally:
                session.close()
        except Exception:
            return []

    def prefers_vision_understanding(self, model_key: str | None) -> bool:
        if not model_key:
            return False
        try:
            from sqlalchemy import select

            from backend.models.llm_model import LLMModelConfig

            session = self._session_factory()
            try:
                selected = session.scalar(
                    select(LLMModelConfig).where(
                        LLMModelConfig.model_key == model_key,
                        LLMModelConfig.is_enabled.is_(True),
                        LLMModelConfig.capability_vision.is_(True),
                        LLMModelConfig.vision_understanding_priority.is_(True),
                    )
                )
                return selected is not None
            finally:
                session.close()
        except Exception:
            return False

    def _database_image_provider_for_model(self, model_key: str | None) -> list[ProviderState]:
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from backend.models.llm_model import LLMModelConfig

            session = self._session_factory()
            try:
                selected = None
                if model_key:
                    try:
                        selected = session.scalar(
                            select(LLMModelConfig)
                            .options(selectinload(LLMModelConfig.provider_account))
                            .where(
                                LLMModelConfig.model_key == model_key,
                                LLMModelConfig.is_enabled.is_(True),
                                LLMModelConfig.capability_vision.is_(True),
                            )
                        )
                    except Exception:
                        selected = None
                providers: list[ProviderState] = []
                if selected and selected.provider_account and selected.provider_account.is_enabled:
                    account = selected.provider_account
                    self._append_unique_provider(
                        providers,
                        ProviderState(
                            name=account.provider_name,
                            base_url=account.base_url,
                            api_key=account.api_key,
                            model=selected.provider_model,
                        ),
                    )

                vision_rows = session.scalars(
                    select(LLMModelConfig)
                    .options(selectinload(LLMModelConfig.provider_account))
                    .where(
                        LLMModelConfig.is_enabled.is_(True),
                        LLMModelConfig.capability_vision.is_(True),
                    )
                    .order_by(LLMModelConfig.sort_order.asc(), LLMModelConfig.id.asc())
                ).all()
                for item in vision_rows:
                    account = item.provider_account
                    if not account or not account.is_enabled:
                        continue
                    self._append_unique_provider(
                        providers,
                        ProviderState(
                            name=account.provider_name,
                            base_url=account.base_url,
                            api_key=account.api_key,
                            model=item.provider_model,
                        ),
                    )
                return providers
            finally:
                session.close()
        except Exception:
            return []

    async def generate_response(self, messages: list[dict[str, str]], fallback_text: str) -> str:
        chunks: list[str] = []
        async for chunk in self.stream_response(messages, fallback_text):
            chunks.append(chunk)
        return "".join(chunks).strip()

    async def complete_response(
        self,
        messages: list[dict[str, object]],
        fallback_text: str = "",
        *,
        model_key: str | None = None,
        max_completion_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> str:
        selected_model_key = self.normalize_chat_model_key(model_key)
        for provider in self._providers_for_chat_model(selected_model_key):
            if not provider.available or not provider.base_url or not provider.api_key:
                continue
            try:
                text = await self._complete_openai_compatible(
                    provider,
                    messages,
                    max_completion_tokens=max_completion_tokens,
                    temperature=temperature,
                    timeout_seconds=self.settings.llm_request_timeout_seconds,
                    client_kind="chat",
                )
                if text.strip():
                    self._reset_provider(provider)
                    return text.strip()
                self._mark_provider_failure(provider)
            except Exception as exc:
                logger.warning(
                    "llm_completion_provider_failure provider=%s model=%s model_key=%s error_type=%s error=%s",
                    provider.name,
                    provider.model,
                    selected_model_key,
                    type(exc).__name__,
                    str(exc)[:300],
                )
                self._mark_provider_failure(provider)
        return fallback_text.strip()

    async def stream_response(
        self,
        messages: list[dict[str, str]],
        fallback_text: str,
        *,
        model_key: str | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_events(messages, fallback_text, model_key=model_key):
            if event.type == "chunk":
                yield event.content

    async def stream_events(
        self,
        messages: list[dict[str, str]],
        fallback_text: str,
        *,
        model_key: str | None = None,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        selected_model_key = self.normalize_chat_model_key(model_key)
        fallback_reason = "no_available_provider"
        for provider in self._providers_for_chat_model(selected_model_key):
            if not provider.available or not provider.base_url or not provider.api_key:
                continue
            try:
                yielded_text = False
                pending_usage_events: list[LLMStreamEvent] = []
                stream_func = getattr(self._stream_openai_compatible, "__func__", None)
                if stream_func is not LLMService._stream_openai_compatible:
                    async for chunk in self._stream_openai_compatible(provider, messages):
                        if not chunk:
                            continue
                        yielded_text = True
                        yield LLMStreamEvent(
                            type="chunk",
                            content=chunk,
                            provider_name=provider.name,
                            provider_model=provider.model,
                        )
                else:
                    async for event in self._stream_openai_compatible_events(
                        provider,
                        messages,
                        max_completion_tokens=max_completion_tokens,
                        temperature=temperature,
                        top_p=top_p,
                    ):
                        if event.type == "usage":
                            if yielded_text:
                                yield event
                            else:
                                pending_usage_events.append(event)
                            continue
                        if not event.content:
                            continue
                        yielded_text = True
                        yield event
                        if pending_usage_events:
                            for usage_event in pending_usage_events:
                                yield usage_event
                            pending_usage_events.clear()
                if yielded_text:
                    self._reset_provider(provider)
                    return
                fallback_reason = "empty_stream"
                llm_stream_provider_failure_total.labels(
                    provider=provider.name,
                    model_key=selected_model_key,
                    reason=fallback_reason,
                ).inc()
                logger.warning(
                    "llm_stream_provider_failure reason=%s provider=%s model=%s model_key=%s",
                    fallback_reason,
                    provider.name,
                    provider.model,
                    selected_model_key,
                )
                self._mark_provider_failure(provider)
            except Exception as exc:
                fallback_reason = "provider_exception"
                llm_stream_provider_failure_total.labels(
                    provider=provider.name,
                    model_key=selected_model_key,
                    reason=fallback_reason,
                ).inc()
                logger.warning(
                    "llm_stream_provider_failure reason=%s provider=%s model=%s model_key=%s error_type=%s error=%s",
                    fallback_reason,
                    provider.name,
                    provider.model,
                    selected_model_key,
                    type(exc).__name__,
                    str(exc)[:300],
                )
                self._mark_provider_failure(provider)

        if fallback_text:
            llm_stream_fallback_total.labels(model_key=selected_model_key, reason=fallback_reason).inc()
            logger.warning("llm_stream_fallback reason=%s model_key=%s", fallback_reason, selected_model_key)
            yield LLMStreamEvent(type="chunk", content=fallback_text)

    async def extract_image_text(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        model_key: str | None = None,
    ) -> str:
        prompt = (
            f"你现在执行高中{subject}题目图片的 OCR。"
            "只提取图片里能明确看清的文字、数字、公式、标签。"
            "不要解释，不要总结，不要补全看不清的内容。"
            "如果看不清，只输出能确认的部分。"
        )
        return await self._generate_image_completion(
            prompt=prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
            model_key=model_key,
        )

    async def summarize_academic_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        user_text: str,
        ocr_text: str,
        model_key: str | None = None,
    ) -> str:
        prompt = (
            f"你在辅助理解一张高中{subject}题目图片。"
            "请只输出 JSON 单对象，UTF-8，无 markdown 围栏。"
            "不要直接给最终答案，不要编造看不清的内容。"
            "JSON 字段必须包含："
            '"is_academic" (boolean), '
            '"subject_guess" (string), '
            '"question_text" (string), '
            '"known_conditions" (string array), '
            '"options" (object，键为 A/B/C/D 等选项号), '
            '"formulas_latex" (string array), '
            '"diagrams" (array of objects，每项含 type 和 description), '
            '"handwriting" (string), '
            '"printed_answer" (string，图中印刷的参考答案/解析，无则空), '
            '"uncertainties" (string array), '
            '"quality_issues" (array，只能使用 blur/dark/incomplete/glare)。'
            "如果图片不是高中学科题目，is_academic=false，其余字段尽量留空，并在 uncertainties 说明原因。"
            f"学生补充文字：{user_text or '（无）'}。"
            f"OCR 提取：{ocr_text or '（无）'}。"
        )
        return await self._generate_image_completion(
            prompt=prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
            model_key=model_key,
        )

    def _reset_provider(self, provider: ProviderState) -> None:
        provider.failures = 0
        provider.open_until = None

    def _mark_provider_failure(self, provider: ProviderState) -> None:
        provider.failures += 1
        if provider.failures >= self.settings.llm_circuit_breaker_threshold:
            provider.open_until = datetime.now(UTC) + timedelta(seconds=self.settings.llm_circuit_breaker_seconds)

    async def _stream_openai_compatible(self, provider: ProviderState, messages: list[dict[str, str]]):
        async for event in self._stream_openai_compatible_events(provider, messages):
            if event.type == "chunk":
                yield event.content

    async def _stream_openai_compatible_events(
        self,
        provider: ProviderState,
        messages: list[dict[str, str]],
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        content_filter = ThinkingContentFilter()
        emitted_text = ""
        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else 0.3,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if max_completion_tokens:
            payload["max_completion_tokens"] = max_completion_tokens
        url = self._chat_completions_url(provider.base_url)
        raw_event_count = 0
        content_event_count = 0
        reasoning_event_count = 0
        usage_event_count = 0
        finish_reasons: list[str] = []
        client = await self._get_chat_http_client()
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                payload = json.loads(data)
                raw_event_count += 1
                usage = self._parse_usage(payload)
                if usage is not None:
                    usage_event_count += 1
                    yield LLMStreamEvent(
                        type="usage",
                        usage=usage,
                        provider_name=provider.name,
                        provider_model=provider.model,
                    )
                    continue
                choice = payload.get("choices", [{}])[0]
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    finish_reasons.append(str(finish_reason))
                delta = _content_text(choice.get("delta", {}).get("content"))
                message_content = _content_text(choice.get("message", {}).get("content"))
                reasoning_content = _content_text(choice.get("delta", {}).get("reasoning")) or _content_text(
                    choice.get("delta", {}).get("reasoning_content")
                )
                if reasoning_content:
                    reasoning_event_count += 1
                text = delta or message_content
                if text and emitted_text and text.startswith(emitted_text):
                    text = text[len(emitted_text) :]
                if text:
                    content_event_count += 1
                    visible_text = content_filter.feed(text)
                    if visible_text:
                        emitted_text += visible_text
                        yield LLMStreamEvent(
                            type="chunk",
                            content=visible_text,
                            provider_name=provider.name,
                            provider_model=provider.model,
                        )

        final_visible_text = content_filter.flush()
        if final_visible_text:
            emitted_text += final_visible_text
            yield LLMStreamEvent(
                type="chunk",
                content=final_visible_text,
                provider_name=provider.name,
                provider_model=provider.model,
            )
        if not emitted_text:
            logger.warning(
                "llm_stream_no_visible_content provider=%s model=%s raw_events=%s content_events=%s reasoning_events=%s usage_events=%s finish_reasons=%s",
                provider.name,
                provider.model,
                raw_event_count,
                content_event_count,
                reasoning_event_count,
                usage_event_count,
                ",".join(finish_reasons),
            )

    @staticmethod
    def _parse_usage(payload: dict) -> LLMUsage | None:
        raw_usage = payload.get("usage")
        if not isinstance(raw_usage, dict):
            return None
        completion_details = raw_usage.get("completion_tokens_details")
        prompt_details = raw_usage.get("prompt_tokens_details")
        return LLMUsage(
            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
            total_tokens=int(raw_usage.get("total_tokens") or 0),
            reasoning_tokens=int(
                raw_usage.get("reasoning_tokens")
                or (completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else 0)
                or 0
            ),
            prompt_cache_hit_tokens=int(
                raw_usage.get("prompt_cache_hit_tokens")
                or (prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else 0)
                or 0
            ),
            prompt_cache_miss_tokens=int(raw_usage.get("prompt_cache_miss_tokens") or 0),
        )

    async def _generate_image_completion(
        self,
        *,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        model_key: str | None = None,
    ) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": self._image_data_url(image_bytes=image_bytes, mime_type=mime_type)},
                    },
                ],
            }
        ]
        for provider in self._image_completion_providers(model_key):
            if not provider.available or not provider.base_url or not provider.api_key:
                continue
            try:
                started_at = monotonic()
                text = await self._complete_openai_compatible(provider, messages)
                if text and not self._looks_like_no_image_received(text):
                    self._reset_provider(provider)
                    return text.strip()
            except Exception as exc:
                duration_seconds = monotonic() - started_at
                reason = self._image_call_failure_reason(exc)
                chat_image_vision_call_failures_total.labels(reason=reason).inc()
                logger.warning(
                    "chat_image_vision_call_failure provider=%s model=%s error_type=%s http_status=%s duration_seconds=%.3f",
                    provider.name,
                    provider.model,
                    type(exc).__name__,
                    self._http_status_code(exc),
                    duration_seconds,
                )
                self._mark_provider_failure(provider)
        return ""

    def _image_completion_providers(self, model_key: str | None) -> list[ProviderState]:
        return self._database_image_provider_for_model(model_key)

    @staticmethod
    def _append_unique_provider(providers: list[ProviderState], provider: ProviderState) -> None:
        if not any(
            existing.name == provider.name
            and existing.base_url == provider.base_url
            and existing.model == provider.model
            for existing in providers
        ):
            providers.append(provider)

    @staticmethod
    def _looks_like_no_image_received(text: str) -> bool:
        normalized = re.sub(r"\s+", "", (text or "").strip())
        return bool(normalized) and any(pattern.search(normalized) for pattern in NO_IMAGE_RECEIVED_PATTERNS)

    @staticmethod
    def _http_status_code(exc: Exception) -> int | None:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            return exc.response.status_code
        return None

    @classmethod
    def _image_call_failure_reason(cls, exc: Exception) -> str:
        status_code = cls._http_status_code(exc)
        if status_code is not None:
            if 400 <= status_code < 500:
                return "http_4xx"
            if status_code >= 500:
                return "http_5xx"
            return "other"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.RequestError):
            return "network"
        return "other"

    async def _complete_openai_compatible(
        self,
        provider: ProviderState,
        messages: list[dict[str, object]],
        *,
        max_completion_tokens: int | None = None,
        temperature: float = 0.1,
        timeout_seconds: float | None = None,
        client_kind: Literal["chat", "vision"] = "vision",
    ) -> str:
        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_completion_tokens:
            payload["max_completion_tokens"] = max_completion_tokens
        url = self._chat_completions_url(provider.base_url)
        timeout = httpx.Timeout(timeout_seconds or self.settings.effective_chat_image_vision_timeout_seconds)
        client = (
            await self._get_chat_http_client()
            if client_kind == "chat"
            else await self._get_vision_http_client()
        )
        response = await client.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        message = body.get("choices", [{}])[0].get("message", {})
        return _content_text(message.get("content", ""))

    @staticmethod
    def _chat_completions_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        if normalized.endswith("/v1"):
            return f"{normalized}/chat/completions"
        return f"{normalized}/v1/chat/completions"

    @staticmethod
    def _image_data_url(*, image_bytes: bytes, mime_type: str) -> str:
        return f"data:{mime_type};base64,{b64encode(image_bytes).decode('ascii')}"


llm_service = LLMService()
