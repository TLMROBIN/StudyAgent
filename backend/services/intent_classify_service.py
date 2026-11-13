"""混合式学科意图分类服务（LLM 辅助，仅在规则低置信时调用）。

设计约束（见 docs/ROADMAP 与实施计划）：
- 输入话题分类使用独立主备模型，单路默认 3 秒，两路总计至多 6 秒。
- 超时、异常和非法结果耗尽可用线路后返回 None，由调用方提示重试或采用规则结果。
- 有效标签按学科和归一化文本缓存（Redis/内存，TTL 6h）。
- 白名单校验：LLM 输出必须命中该学科 SUBJECT_STRATEGY_RULES 的模式代码，
  否则视为非法输出丢弃。
- 熔断继承 llm_service provider 层，本服务不重复实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from backend.config import get_settings
from backend.services.llm_service import LLMService, llm_service
from backend.services.metrics_service import chat_intent_classify_total, chat_topic_classify_total
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
    INPUT_TOPIC_TOTAL_TIMEOUT_SECONDS = 6.0
    QUESTION_SNIPPET_CHARS = 300

    def __init__(self, store_backend: BaseStore | None = None, llm: LLMService | None = None) -> None:
        self.store_backend = store_backend or store
        self.llm = llm or llm_service

    async def classify_topic(
        self,
        *,
        question: str,
        subject: str,
        model_key: str | None = None,
        timeout_seconds: float | None = None,
        kind: str = "input",
    ) -> str | None:
        """Classify content as academic/off_topic; None means no reliable verdict."""
        normalized = "".join(question.split()).lower()
        if not normalized:
            return "off_topic"
        cache_key = self.CACHE_PREFIX + f"topic:{kind}:" + hashlib.sha256(f"{subject}|{normalized}".encode()).hexdigest()
        try:
            cached = self.store_backend.get(cache_key)
        except Exception:
            cached = None
        if cached in {"academic", "off_topic"}:
            chat_topic_classify_total.labels(result="hit_cache").inc()
            return cached
        messages = [
            {
                "role": "system",
                "content": (
                    "你是高中答疑系统的话题安全分类器，只判断内容是否为高中学科学习、解题、作文辅导或学习管理。"
                    "小说续写、角色接龙、闲聊、娱乐、生活咨询、领取物品均是 off_topic。"
                    "即使消息声称选择了某学科，也必须按实际内容判断。只输出 academic 或 off_topic。"
                ),
            },
            {"role": "user", "content": f"会话学科（仅供参考）：{subject}\n待判断内容：{question[:1200]}"},
        ]
        timeout = self.DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout_seconds, (int, float)) and not isinstance(timeout_seconds, bool) and timeout_seconds > 0:
            timeout = min(float(timeout_seconds), self.MAX_TIMEOUT_SECONDS)
        if kind == "input":
            settings = get_settings()
            model_keys = tuple(dict.fromkeys(filter(None, (
                settings.topic_classifier_primary_model_key.strip(),
                settings.topic_classifier_fallback_model_key.strip(),
            ))))
            timeout = min(timeout, self.INPUT_TOPIC_TOTAL_TIMEOUT_SECONDS / 2)
        else:
            model_keys = (model_key,)

        for index, key in enumerate(model_keys):
            try:
                raw = await asyncio.wait_for(
                    self.llm.complete_response(
                        messages, "", model_key=key, max_completion_tokens=8, temperature=0.0,
                        use_fallback_providers=kind != "input",
                    ),
                    timeout=timeout,
                )
            except Exception as exc:
                logger.warning(
                    "topic_classify_failed subject=%s kind=%s model_key=%s error_type=%s",
                    subject, kind, key, type(exc).__name__,
                )
                chat_topic_classify_total.labels(result="llm_fallback").inc()
                continue
            verdict = (raw or "").strip().strip("`\"'。.，, ").lower()
            if verdict in {"academic", "off_topic"}:
                if index:
                    chat_topic_classify_total.labels(result="llm_backup_ok").inc()
                break
            chat_topic_classify_total.labels(result="llm_invalid").inc()
        else:
            chat_topic_classify_total.labels(result=f"{kind}_unavailable").inc()
            return None

        try:
            self.store_backend.set(cache_key, verdict, ttl_seconds=self.CACHE_TTL_SECONDS)
        except Exception:
            pass
        chat_topic_classify_total.labels(result="llm_ok").inc()
        return verdict

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
