from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from typing import AsyncIterator

import httpx

from backend.config import get_settings

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

    async def generate_response(self, messages: list[dict[str, str]], fallback_text: str) -> str:
        chunks: list[str] = []
        async for chunk in self.stream_response(messages, fallback_text):
            chunks.append(chunk)
        return "".join(chunks).strip()

    async def stream_response(self, messages: list[dict[str, str]], fallback_text: str) -> AsyncIterator[str]:
        for provider in self.providers:
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


llm_service = LLMService()
