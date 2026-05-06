from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from base64 import b64encode
import json
from typing import AsyncIterator

import httpx

from backend.config import get_settings
from backend.database import SessionLocal

OPEN_THINK_TAG = "<think>"
CLOSE_THINK_TAG = "</think>"


def _partial_tag_suffix_length(text: str, tag: str) -> int:
    max_size = min(len(text), len(tag) - 1)
    for size in range(max_size, 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


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


class LLMService:
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

    def _runtime_providers(self) -> list[ProviderState]:
        configured = self._database_providers()
        return configured or self.providers

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

    async def generate_response(self, messages: list[dict[str, str]], fallback_text: str) -> str:
        chunks: list[str] = []
        async for chunk in self.stream_response(messages, fallback_text):
            chunks.append(chunk)
        return "".join(chunks).strip()

    async def stream_response(self, messages: list[dict[str, str]], fallback_text: str) -> AsyncIterator[str]:
        for provider in self._runtime_providers():
            if not provider.available or not provider.base_url or not provider.api_key:
                continue
            try:
                yielded = False
                async for chunk in self._stream_openai_compatible(provider, messages):
                    yielded = True
                    yield chunk
                if yielded:
                    self._reset_provider(provider)
                    return
            except Exception:
                self._mark_provider_failure(provider)

        if fallback_text:
            yield fallback_text

    async def extract_image_text(self, *, image_bytes: bytes, mime_type: str, subject: str) -> str:
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
        )

    async def summarize_academic_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        user_text: str,
        ocr_text: str,
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
        )

    def _reset_provider(self, provider: ProviderState) -> None:
        provider.failures = 0
        provider.open_until = None

    def _mark_provider_failure(self, provider: ProviderState) -> None:
        provider.failures += 1
        if provider.failures >= self.settings.llm_circuit_breaker_threshold:
            provider.open_until = datetime.now(UTC) + timedelta(seconds=self.settings.llm_circuit_breaker_seconds)

    async def _stream_openai_compatible(self, provider: ProviderState, messages: list[dict[str, str]]):
        content_filter = ThinkingContentFilter()
        headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": 0.3,
            "stream": True,
        }
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
                    choice = payload.get("choices", [{}])[0]
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        visible_text = content_filter.feed(delta)
                        if visible_text:
                            yield visible_text

        final_visible_text = content_filter.flush()
        if final_visible_text:
            yield final_visible_text

    async def _generate_image_completion(self, *, prompt: str, image_bytes: bytes, mime_type: str) -> str:
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
        for provider in self._runtime_providers():
            if not provider.available or not provider.base_url or not provider.api_key:
                continue
            try:
                text = await self._complete_openai_compatible(provider, messages)
                if text:
                    self._reset_provider(provider)
                    return text.strip()
            except Exception:
                self._mark_provider_failure(provider)
        return ""

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
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        return str(content or "")

    @staticmethod
    def _image_data_url(*, image_bytes: bytes, mime_type: str) -> str:
        return f"data:{mime_type};base64,{b64encode(image_bytes).decode('ascii')}"


llm_service = LLMService()
