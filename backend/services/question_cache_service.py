from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from backend.config import get_settings
from backend.models.conversation import GuidanceStage
from backend.models.knowledge import KnowledgeChunk
from backend.services.metrics_service import record_chat_cache_lookup
from backend.services.store_service import BaseStore, store

_QUESTION_NORMALIZE_PATTERN = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass
class QuestionCacheLookup:
    cache_key: str | None
    answer: str | None


class QuestionCacheService:
    def __init__(self, store_backend: BaseStore | None = None) -> None:
        self.settings = get_settings()
        self.store_backend = store_backend or store

    def lookup(
        self,
        *,
        subject: str,
        question: str,
        guidance_stage: GuidanceStage,
        agent_version: int,
        chunks: list[KnowledgeChunk],
        llm_model: str | None = None,
    ) -> QuestionCacheLookup:
        cache_key = self._build_key(
            subject=subject,
            question=question,
            guidance_stage=guidance_stage,
            agent_version=agent_version,
            chunks=chunks,
            llm_model=llm_model,
        )
        if not cache_key:
            return QuestionCacheLookup(cache_key=None, answer=None)

        cached = self.store_backend.get(cache_key)
        record_chat_cache_lookup(hit=bool(cached))
        return QuestionCacheLookup(cache_key=cache_key, answer=cached)

    def store_answer(self, cache_key: str | None, answer: str) -> None:
        if not cache_key or not answer.strip():
            return
        self.store_backend.set(
            cache_key,
            answer,
            ttl_seconds=self.settings.hot_question_cache_ttl_seconds,
        )

    def is_cacheable(self, *, history_pairs: list[tuple[str, str]], question: str, has_image_turn: bool = False) -> bool:
        return not has_image_turn and not history_pairs and len(self.normalize_question(question)) >= 6

    @staticmethod
    def normalize_question(question: str) -> str:
        lowered = question.strip().lower()
        normalized = _QUESTION_NORMALIZE_PATTERN.sub("", lowered)
        return normalized

    def _build_key(
        self,
        *,
        subject: str,
        question: str,
        guidance_stage: GuidanceStage,
        agent_version: int,
        chunks: list[KnowledgeChunk],
        llm_model: str | None = None,
    ) -> str | None:
        normalized_question = self.normalize_question(question)
        if not normalized_question:
            return None

        payload = {
            "subject": subject.strip(),
            "question": normalized_question,
            "guidance_stage": guidance_stage.value,
            "agent_version": agent_version,
            "chunk_ids": [chunk.id for chunk in chunks],
            "llm_model": (llm_model or "").strip(),
        }
        digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return f"question_cache:{digest}"


question_cache_service = QuestionCacheService()
