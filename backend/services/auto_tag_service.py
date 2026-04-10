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
        if not vocabulary:
            return list(existing_tags or [])

        clean_title = self._clean_title(filename)
        if len(clean_title) < _MIN_TITLE_LENGTH:
            return list(existing_tags or [])

        matched = self._match_tags(clean_title, vocabulary)

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
        from backend.models.knowledge import KnowledgeDocument, ResourceType

        rows = db.execute(
            select(KnowledgeDocument.tags_json, KnowledgeDocument.subject).where(
                KnowledgeDocument.resource_type == ResourceType.TEXTBOOK.value
            )
        ).all()

        by_subject: dict[str, set[str]] = {}
        for tags_json, subject in rows:
            if not tags_json or not subject:
                continue
            by_subject.setdefault(subject, set())
            for tag in tags_json:
                tag = str(tag).strip()
                if len(tag) >= _MIN_TAG_LENGTH:
                    by_subject[subject].add(tag)

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


auto_tag_service = AutoTagService()
