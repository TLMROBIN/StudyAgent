"""混合式学科意图分类服务（LLM 辅助，仅在规则低置信时调用）。

设计约束（见 docs/ROADMAP 与实施计划）：
- fail-open：LLM 超时/异常/非法输出一律返回 None，调用方回退纯规则结果，
  绝不因分类失败阻断答疑链路。
- 有界延迟：asyncio.wait_for 限时（默认 3s，上限 5s），结果按
  sha256(subject + 归一化问题) 缓存（Redis/内存，TTL 6h）。
- 白名单校验：LLM 输出必须命中该学科 SUBJECT_STRATEGY_RULES 的模式代码，
  否则视为非法输出丢弃。
- 熔断继承 llm_service provider 层，本服务不重复实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from backend.services.llm_service import LLMService, llm_service
from backend.services.metrics_service import chat_intent_classify_total
from backend.services.store_service import BaseStore, store
from backend.services.subject_guidance_service import (
    SUBJECT_STRATEGY_RULES,
    SubjectTeachingMode,
)

logger = logging.getLogger(__name__)


class IntentClassifyService:
    CACHE_PREFIX = "intent_cls:"
    CACHE_TTL_SECONDS = 6 * 3600
    DEFAULT_TIMEOUT_SECONDS = 3.0
    MAX_TIMEOUT_SECONDS = 5.0
    QUESTION_SNIPPET_CHARS = 300

    def __init__(self, store_backend: BaseStore | None = None, llm: LLMService | None = None) -> None:
        self.store_backend = store_backend or store
        self.llm = llm or llm_service

    async def classify(
        self,
        *,
        question: str,
        subject: str,
        model_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SubjectTeachingMode | None:
        rules = SUBJECT_STRATEGY_RULES.get(subject)
        if not rules:
            return None
        candidates = {rule.mode.value: rule for rule in rules}
        normalized = "".join(question.split()).lower()
        if not normalized:
            return None

        cache_key = self.CACHE_PREFIX + hashlib.sha256(f"{subject}|{normalized}".encode("utf-8")).hexdigest()
        try:
            cached = self.store_backend.get(cache_key)
        except Exception:  # 缓存后端故障不影响分类
            cached = None
        if cached is not None:
            chat_intent_classify_total.labels(result="hit_cache").inc()
            return SubjectTeachingMode(cached) if cached in candidates else None

        options = "\n".join(
            f"- {mode_value}: {rule.prompt_section.split('：', 1)[0]}" for mode_value, rule in candidates.items()
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是高中答疑系统的意图分类器。根据学生问题为其选择最合适的教学模式。"
                    "只输出一个模式代码（如 math_problem），不要解释，不要输出其他内容。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"学科：{subject}\n"
                    f"学生问题：{question.strip()[: self.QUESTION_SNIPPET_CHARS]}\n"
                    f"候选教学模式：\n{options}\n"
                    "只输出一个模式代码："
                ),
            },
        ]
        timeout = self.DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout_seconds, (int, float)) and not isinstance(timeout_seconds, bool) and timeout_seconds > 0:
            timeout = min(float(timeout_seconds), self.MAX_TIMEOUT_SECONDS)
        try:
            raw = await asyncio.wait_for(
                self.llm.complete_response(
                    messages,
                    "",
                    model_key=model_key,
                    max_completion_tokens=16,
                    temperature=0.0,
                ),
                timeout=timeout,
            )
        except Exception as exc:  # 超时/网络/provider 异常一律 fail-open
            logger.warning(
                "intent_classify_failed subject=%s error_type=%s error=%s",
                subject,
                type(exc).__name__,
                str(exc)[:200],
            )
            chat_intent_classify_total.labels(result="llm_fallback").inc()
            return None

        answer = (raw or "").strip().strip("`\"'。.，, ").lower()
        matched = candidates.get(answer)
        if matched is None:
            matched = next((rule for value, rule in candidates.items() if value in answer), None)
        if matched is None:
            chat_intent_classify_total.labels(result="llm_invalid").inc()
            return None
        try:
            self.store_backend.set(cache_key, matched.mode.value, ttl_seconds=self.CACHE_TTL_SECONDS)
        except Exception:
            pass
        chat_intent_classify_total.labels(result="llm_ok").inc()
        return matched.mode


intent_classify_service = IntentClassifyService()
