"""Auto-tag service: match lecture-note filenames against textbook tag vocabulary.

Strategy: substring matching, longer tags first. Shorter tags fully contained
in already-matched tags are skipped to avoid redundancy.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_MIN_TAG_LENGTH = 2
_MIN_TITLE_LENGTH = 2
_TAG_PREFIX_PATTERNS = [
    re.compile(r"^\s*第[一二三四五六七八九十百零两0-9]+(?:章|节|课|单元|编|部分)\s*"),
    re.compile(r"^\s*[（(][一二三四五六七八九十百零两0-9]+[)）]\s*"),
    re.compile(r"^\s*[A-Za-z][.、]\s*"),
    re.compile(r"^\s*[0-9]{1,2}(?:\.[0-9]{1,2})*[.．、:：)]\s*"),
]
_GENERIC_TITLE_SUFFIX_PATTERNS = [
    re.compile(r"\s*[（(][A-Za-z0-9 .,_+\-]+[)）]\s*"),
    re.compile(
        r"\s*(?:深度思维导图自学手册|深度思维向导手册|深度思维向导自学手册|深度自学手册|思维向导自学手册|思维向导手册|课程思维向导|课程导学手册|课程学习手册|学习手册|导学手册|自学手册|思维向导|深度报告)\s*$"
    ),
    re.compile(r"的[^的]{0,60}(?:手册|向导|解析|导图|报告).*$"),
]
_GENERIC_TITLE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:(?:课程|物理|数学|化学|生物|英语|语文|政治|历史|地理)?(?:思维向导|课程思维向导|课程导学手册|课程学习手册|学习手册|导学手册))\s*[：:]?\s*"
)

_FILENAME_NOISE_PATTERNS = [
    re.compile(r"\(\d+\)"),
    re.compile(r"_v?\d+(?:\.\d+)*"),
    re.compile(r"\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?"),
    re.compile(r"\s*[副本复件]\s*$"),
    re.compile(r"\s*copy\s*$", re.IGNORECASE),
]


class AutoTagService:
    def __init__(self, cache_ttl: int = 300) -> None:
        self._cache: dict[str, list[str]] = {}
        self._cache_time: float = 0
        self._cache_ttl = cache_ttl

    def auto_tag(
        self,
        db: Session,
        filename: str,
        subject: str,
        existing_tags: list[str] | None = None,
    ) -> list[str]:
        """Match *filename* against textbook tag vocabulary. Preserves *existing_tags*, appends auto-matched tags (max 20 total)."""
        vocabulary = self._get_vocabulary(db, subject)
        clean_title = self._clean_title(filename)
        if len(clean_title) < _MIN_TITLE_LENGTH:
            return list(existing_tags or [])

        matched = self._match_tags(clean_title, vocabulary) if vocabulary else []
        if not matched and not existing_tags:
            matched = self._extract_title_tags(clean_title)

        merged = list(existing_tags or [])
        existing_lower = {t.lower() for t in merged}
        for tag in matched:
            if tag.lower() not in existing_lower:
                merged.append(tag)
                existing_lower.add(tag.lower())

        return merged[:20]

    def invalidate_cache(self) -> None:
        self._cache_time = 0

    def _get_vocabulary(self, db: Session, subject: str) -> list[str]:
        now = time.time()
        if now - self._cache_time > self._cache_ttl:
            self._refresh_cache(db)
        return self._cache.get(subject, [])

    def _refresh_cache(self, db: Session) -> None:
        from backend.models.knowledge import (
            KnowledgeChunk,
            KnowledgeDocument,
            ResourceType,
        )

        document_rows = db.execute(
            select(
                KnowledgeDocument.tags_json,
                KnowledgeDocument.subject,
                KnowledgeDocument.chapter,
                KnowledgeDocument.section,
            ).where(
                KnowledgeDocument.resource_type == ResourceType.TEXTBOOK.value
            )
        ).all()
        chunk_rows = db.execute(
            select(KnowledgeChunk.metadata_json, KnowledgeDocument.subject)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(KnowledgeDocument.resource_type == ResourceType.TEXTBOOK.value)
        ).all()

        by_subject: dict[str, set[str]] = {}
        for tags_json, subject, chapter, section in document_rows:
            if not subject:
                continue
            by_subject.setdefault(subject, set())
            for candidate in [
                *(tags_json or []),
                chapter,
                section,
            ]:
                normalized = self._normalize_tag_candidate(candidate)
                if normalized:
                    by_subject[subject].add(normalized)

        for metadata_json, subject in chunk_rows:
            if not subject or not metadata_json:
                continue
            by_subject.setdefault(subject, set())
            for candidate in [
                metadata_json.get("chapter"),
                metadata_json.get("section"),
            ]:
                normalized = self._normalize_tag_candidate(candidate)
                if normalized:
                    by_subject[subject].add(normalized)

        self._cache = {}
        for subject, tags in by_subject.items():
            self._cache[subject] = sorted(tags, key=len, reverse=True)

        self._cache_time = time.time()
        total = sum(len(v) for v in self._cache.values())
        logger.info(
            "Auto-tag vocabulary refreshed: %d subjects, %d total tags",
            len(self._cache),
            total,
        )

    def _clean_title(self, filename: str) -> str:
        title = filename
        if "." in title:
            title = title.rsplit(".", 1)[0]
        for pattern in _FILENAME_NOISE_PATTERNS:
            title = pattern.sub("", title)
        return re.sub(r"\s+", " ", title).strip()

    def _normalize_tag_candidate(self, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if len(normalized) < _MIN_TAG_LENGTH:
            return None

        for pattern in _TAG_PREFIX_PATTERNS:
            normalized = pattern.sub("", normalized).strip()

        normalized = re.sub(r"\s+", " ", normalized).strip("：:—-·•,，.。 ")
        if len(normalized) < _MIN_TAG_LENGTH:
            return None
        return normalized

    def _match_tags(self, title: str, vocabulary: list[str]) -> list[str]:
        matched: list[str] = []
        matched_lower: list[str] = []
        title_lower = title.lower()

        for tag in vocabulary:
            tag_lower = tag.lower()
            if tag_lower not in title_lower:
                continue
            if any(tag_lower in m for m in matched_lower):
                continue
            matched.append(tag)
            matched_lower.append(tag_lower)

        return matched

    def _extract_title_tags(self, title: str) -> list[str]:
        candidates: list[str] = []
        candidates.extend(match.strip() for match in re.findall(r"《([^》]+)》", title))
        if "：" in title or ":" in title:
            candidates.append(re.split(r"[：:]", title, maxsplit=1)[-1].strip())
        candidates.append(title)

        extracted: list[str] = []
        extracted_lower: set[str] = set()
        for candidate in candidates:
            normalized = self._normalize_title_tag_candidate(candidate)
            if not normalized:
                continue
            key = normalized.lower()
            if key in extracted_lower:
                continue
            if any(key in existing or existing in key for existing in extracted_lower):
                continue
            extracted.append(normalized)
            extracted_lower.add(key)
        return extracted[:5]

    def _normalize_title_tag_candidate(self, value: str | None) -> str | None:
        normalized = self._normalize_tag_candidate(value)
        if not normalized:
            return None

        normalized = normalized.replace("《", "").replace("》", "")
        normalized = _GENERIC_TITLE_PREFIX_PATTERN.sub("", normalized).strip()
        for pattern in _GENERIC_TITLE_SUFFIX_PATTERNS:
            normalized = pattern.sub("", normalized).strip()

        normalized = re.sub(r"\s+", " ", normalized).strip("：:—-·•,，.。 ")
        if len(normalized) < _MIN_TAG_LENGTH:
            return None
        return normalized


auto_tag_service = AutoTagService()
