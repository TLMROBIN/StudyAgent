from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from base64 import b64encode
import json
import logging
import re
from typing import AsyncIterator, Literal

import httpx

from backend.config import get_settings
from backend.database import SessionLocal
from backend.services.metrics_service import llm_stream_fallback_total, llm_stream_provider_failure_total

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
    DEFAULT_CHAT_MODEL_KEY = "minimax-m27"
    LOCAL_VL_CHAT_MODEL_KEY = "qwen2.5-vl"

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
        self._local_vl_provider = ProviderState(
            name=settings.llm_local_vl_name,
            base_url=settings.llm_local_vl_base_url,
            api_key=settings.llm_local_vl_api_key,
            model=settings.llm_local_vl_model,
        )
        self._model_status_cache: dict[str, tuple[datetime, dict[str, str]]] = {}
        self._model_status_ttl_seconds = 300

    def chat_model_options(self) -> list[dict[str, str]]:
        configured = self._database_chat_model_options()
        if configured:
            return configured
        return [
            {
                "key": self.DEFAULT_CHAT_MODEL_KEY,
                "name": "MiniMax-M2.7",
                "description": "highspeed",
            },
            {
                "key": self.LOCAL_VL_CHAT_MODEL_KEY,
                "name": "qwen2.5-vl",
                "description": "图片理解推荐使用，但响应速度可能较慢。",
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
        if selected_key == self.LOCAL_VL_CHAT_MODEL_KEY:
            return [self._local_vl_provider]
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
        url = provider.base_url.rstrip("/") + "/chat/completions"
        timeout = httpx.Timeout(float(self.settings.llm_request_timeout_seconds))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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

    async def generate_response(self, messages: list[dict[str, str]], fallback_text: str) -> str:
        chunks: list[str] = []
        async for chunk in self.stream_response(messages, fallback_text):
            chunks.append(chunk)
        return "".join(chunks).strip()

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
    ) -> AsyncIterator[LLMStreamEvent]:
        selected_model_key = self.normalize_chat_model_key(model_key)
        fallback_reason = "no_available_provider"
        for provider in self._providers_for_chat_model(selected_model_key):
            if not provider.available or not provider.base_url or not provider.api_key:
                continue
            try:
                yielded = False
                stream_func = getattr(self._stream_openai_compatible, "__func__", None)
                if stream_func is not LLMService._stream_openai_compatible:
                    async for chunk in self._stream_openai_compatible(provider, messages):
                        yielded = True
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
                    ):
                        yielded = True
                        yield event
                if yielded:
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
            "请用简洁中文概括图片里能可靠确认的题干、已知条件、图形/电路/实验装置、关键公式或数据。"
            "不要直接给最终答案，不要编造看不清的内容。"
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
    ) -> AsyncIterator[LLMStreamEvent]:
        content_filter = ThinkingContentFilter()
        emitted_text = ""
        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": 0.3,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_completion_tokens:
            payload["max_completion_tokens"] = max_completion_tokens
        url = provider.base_url.rstrip("/") + "/chat/completions"
        timeout = httpx.Timeout(self.settings.llm_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    payload = json.loads(data)
                    usage = self._parse_usage(payload)
                    if usage is not None:
                        yield LLMStreamEvent(
                            type="usage",
                            usage=usage,
                            provider_name=provider.name,
                            provider_model=provider.model,
                        )
                        continue
                    choice = payload.get("choices", [{}])[0]
                    delta = _content_text(choice.get("delta", {}).get("content"))
                    message_content = _content_text(choice.get("message", {}).get("content"))
                    text = delta or message_content
                    if text and emitted_text and text.startswith(emitted_text):
                        text = text[len(emitted_text) :]
                    if text:
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
                text = await self._complete_openai_compatible(provider, messages)
                if text and not self._looks_like_no_image_received(text):
                    self._reset_provider(provider)
                    return text.strip()
            except Exception:
                self._mark_provider_failure(provider)
        return ""

    def _image_completion_providers(self, model_key: str | None) -> list[ProviderState]:
        providers: list[ProviderState] = []

        self._append_unique_provider(providers, self._local_vl_provider)
        return providers

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

    async def _complete_openai_compatible(self, provider: ProviderState, messages: list[dict[str, object]]) -> str:
        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": 0.1,
            "stream": False,
        }
        url = provider.base_url.rstrip("/") + "/chat/completions"
        timeout = httpx.Timeout(self.settings.llm_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        message = body.get("choices", [{}])[0].get("message", {})
        return _content_text(message.get("content", ""))

    @staticmethod
    def _image_data_url(*, image_bytes: bytes, mime_type: str) -> str:
        return f"data:{mime_type};base64,{b64encode(image_bytes).decode('ascii')}"


llm_service = LLMService()
