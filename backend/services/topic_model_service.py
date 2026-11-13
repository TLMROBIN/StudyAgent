"""本地话题决策模型客户端（微调后的 Laya / bge-m3 分类头）。

模型由 scripts/topic_model/serve.py 在局域网内提供 HTTP 接口，学生内容不出内网。
本服务只负责调用与分档，不做最终裁决：
- 返回 None 表示模型不可用（未配置、超时、异常、非法输出），调用方必须回退现有分类；
- 分档 academic / off_topic / uncertain 由阈值决定，阈值来自离线评估，不在此硬编码。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter

import httpx

from backend.config import get_settings
from backend.services.metrics_service import chat_topic_model_seconds, chat_topic_model_total

logger = logging.getLogger(__name__)

TOPIC_MODEL_MODES = {"off", "shadow", "accept_only", "enforce"}
CONTEXT_MAX_CHARS = 300
TEXT_MAX_CHARS = 1200


@dataclass(frozen=True)
class TopicModelResult:
    p_academic: float
    band: str
    model_version: str


class TopicModelService:
    def __init__(
        self,
        *,
        mode: str | None = None,
        url: str | None = None,
        timeout_seconds: float | None = None,
        accept_threshold: float | None = None,
        reject_threshold: float | None = None,
        enforce_output: bool | None = None,
        unix_socket: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        resolved_mode = (mode if mode is not None else settings.topic_model_mode).strip().lower()
        self.url = (url if url is not None else settings.topic_model_url).strip()
        self.mode = resolved_mode if resolved_mode in TOPIC_MODEL_MODES and self.url else "off"
        if resolved_mode not in TOPIC_MODEL_MODES:
            logger.warning("topic_model_mode_invalid mode=%s fallback=off", resolved_mode)
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.topic_model_timeout_seconds
        self.accept_threshold = accept_threshold if accept_threshold is not None else settings.topic_model_accept_threshold
        self.reject_threshold = reject_threshold if reject_threshold is not None else settings.topic_model_reject_threshold
        self.enforce_output = enforce_output if enforce_output is not None else settings.topic_model_enforce_output
        self._unix_socket = unix_socket if unix_socket is not None else settings.topic_model_unix_socket
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def enforces(self, kind: str) -> bool:
        if self.mode == "accept_only":
            return kind == "input"
        return self.mode == "enforce" and (kind == "input" or self.enforce_output)

    def can_decide(self, band: str) -> bool:
        return band == "academic" or (self.mode == "enforce" and band == "off_topic")

    def band_for(self, p_academic: float) -> str:
        if p_academic >= self.accept_threshold:
            return "academic"
        if p_academic <= self.reject_threshold:
            return "off_topic"
        return "uncertain"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = self._transport
            if transport is None and self._unix_socket:
                transport = httpx.AsyncHTTPTransport(uds=self._unix_socket)
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds, transport=transport, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def predict(
        self, *, text: str, subject: str, context: str | None = None,
        kind: str = "input", metric_kind: str | None = None,
    ) -> TopicModelResult | None:
        if not self.enabled:
            return None
        payload = {
            "subject": subject,
            "text": text[:TEXT_MAX_CHARS],
            "context": (context or "")[-CONTEXT_MAX_CHARS:],
            "kind": kind,
        }
        started = perf_counter()
        try:
            response = await self._get_client().post(self.url, json=payload)
            response.raise_for_status()
            body = response.json()
            p_academic = float(body["p_academic"])
            if not 0.0 <= p_academic <= 1.0:
                raise ValueError("p_academic out of range")
        except httpx.TimeoutException:
            chat_topic_model_total.labels(mode=self.mode, kind=metric_kind or kind, result="timeout").inc()
            logger.warning("topic_model_timeout subject=%s kind=%s", subject, kind)
            return None
        except Exception as exc:
            chat_topic_model_total.labels(mode=self.mode, kind=metric_kind or kind, result="error").inc()
            logger.warning("topic_model_failed subject=%s kind=%s error_type=%s", subject, kind, type(exc).__name__)
            return None
        finally:
            chat_topic_model_seconds.observe(perf_counter() - started)
        chat_topic_model_total.labels(mode=self.mode, kind=metric_kind or kind, result="ok").inc()
        return TopicModelResult(
            p_academic=p_academic,
            band=self.band_for(p_academic),
            model_version=str(body.get("model_version") or "unknown")[:64],
        )


topic_model_service = TopicModelService()
