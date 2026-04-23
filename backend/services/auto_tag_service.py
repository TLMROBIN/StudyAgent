"""Auto-tag service: match lecture-note filenames against textbook tag vocabulary.

Strategy: substring matching, longer tags first. Shorter tags fully contained
in already-matched tags are skipped to avoid redundancy.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
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
_CURRICULUM_PREFIX_PATTERNS = [
    re.compile(
        r"^\s*(?:人教版|教科版|鲁科版|粤教版|苏教版)?\s*(?:高中)?\s*(?:语文|数学|英语|物理|化学|生物|政治|历史|地理)?\s*(?:必修|选择性?必修|选修)\s*[一二三四五六七八九十百零两0-9]+(?:册|上|下)?\s*"
    ),
    re.compile(r"^\s*(?:模块|专题)\s*[一二三四五六七八九十百零两0-9]+\s*"),
]
_TRAILING_CURRICULUM_SUFFIX_PATTERNS = [
    re.compile(
        r"\s*[-_－—]\s*(?:[^-_－—\s]+版)?(?:.*?(?:必修|选修|选择性?必修|第[一二三四五六七八九十百零两0-9]+册|上册|下册).*)$"
    ),
]
_STRUCTURE_TAIL_PATTERNS = [
    re.compile(r"(第[一二三四五六七八九十百零两0-9]+(?:节|课)\s*\S.*)$"),
    re.compile(r"([0-9]{1,2}(?:\.[0-9]{1,2})*[.．、]?\s*\S.*)$"),
]
_OUTLINE_PREFIX_PATTERNS = [
    re.compile(r"^\s*\d{1,2}[.．]\d{1,2}\s*[+＋\-—－_:：、.．]?\s*"),
    re.compile(
        r"^\s*第[一二三四五六七八九十百零两0-9]+章\s*第[一二三四五六七八九十百零两0-9]+(?:节|课)\s*"
    ),
]
_FUZZY_SEPARATOR_PATTERN = re.compile(r"[\s\u3000\-_+＋—－–·•,:：，。;；、/\\()（）\[\]【】'\"《》]+")
_FUZZY_CONNECTOR_PATTERN = re.compile(r"[和与及跟]")
_LEADING_DECIMAL_OUTLINE_PATTERN = re.compile(r"^\s*(\d{1,2})[.．](\d{1,2})")
_CHAPTER_NUMBER_PATTERN = re.compile(r"第([一二三四五六七八九十百零两0-9]+)章")
_SECTION_NUMBER_PATTERN = re.compile(r"第([一二三四五六七八九十百零两0-9]+)(?:节|课)")
_DECIMAL_SECTION_NUMBER_PATTERN = re.compile(r"^\s*(\d{1,2})[.．]")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


class AutoTagService:
    def __init__(self, cache_ttl: int = 300) -> None:
        self._cache: dict[str, list[str]] = {}
        self._structure_cache: dict[str, list[dict[str, str | None]]] = {}
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
        self._structure_cache = {}
        self._cache_time = 0

    def match_textbook_structure(
        self,
        db: Session,
        title: str,
        subject: str,
    ) -> dict[str, str | None]:
        now = time.time()
        if now - self._cache_time > self._cache_ttl:
            self._refresh_cache(db)
        clean_title = self._clean_title(title)
        chapter_hint, section_hint, prefers_decimal_sections = self._extract_structure_number_hints(clean_title)
        title_candidates = self._structure_title_candidates(clean_title)
        if not title_candidates:
            return {"chapter": None, "section": None}

        best_match: dict[str, str | None] | None = None
        best_rank: tuple[int, int, int, int] | None = None
        for title_key in title_candidates:
            for index, entry in enumerate(self._structure_cache.get(subject, [])):
                label_key = str(entry.get("match_key") or "").lower()
                if not label_key:
                    continue
                entry_chapter_number = self._as_int(entry.get("chapter_number"))
                entry_section_number = self._as_int(entry.get("section_number"))
                if chapter_hint is not None and entry_chapter_number is not None and entry_chapter_number != chapter_hint:
                    continue
                if (
                    section_hint is not None
                    and str(entry.get("kind") or "") == "section"
                    and entry_section_number is not None
                    and entry_section_number != section_hint
                ):
                    continue
                score = self._structure_match_score(
                    title_key,
                    label_key,
                    kind=str(entry.get("kind") or "chapter"),
                )
                if score is None:
                    continue
                if chapter_hint is not None and entry_chapter_number == chapter_hint:
                    score += 90
                if section_hint is not None and entry_section_number == section_hint:
                    score += 110
                if (
                    prefers_decimal_sections
                    and str(entry.get("kind") or "") == "section"
                    and entry.get("section_style") == "decimal"
                ):
                    score += 15
                rank = (
                    score,
                    len(label_key),
                    1 if str(entry.get("kind") or "") == "section" else 0,
                    -index,
                )
                if best_rank is None or rank > best_rank:
                    best_rank = rank
                    best_match = entry

        if best_match is None:
            best_match = self._fallback_match_by_chapter_number(
                subject_entries=self._structure_cache.get(subject, []),
                chapter_hint=chapter_hint,
                prefers_decimal_sections=prefers_decimal_sections,
            )

        if best_match is None:
            return {"chapter": None, "section": None}

        chapter = str(best_match.get("chapter") or "").strip() or None
        section = None
        if str(best_match.get("kind") or "") == "section":
            section = str(best_match.get("section") or "").strip() or None
        return {"chapter": chapter, "section": section}

    def list_textbook_structure_options(
        self,
        db: Session,
        subject: str,
    ) -> list[dict[str, list[str] | str]]:
        now = time.time()
        if now - self._cache_time > self._cache_ttl:
            self._refresh_cache(db)

        chapter_sections: dict[str, list[str]] = {}
        for entry in self._structure_cache.get(subject, []):
            chapter = str(entry.get("chapter") or "").strip()
            if not chapter:
                continue
            chapter_sections.setdefault(chapter, [])
            if str(entry.get("kind") or "") != "section":
                continue
            section = str(entry.get("section") or "").strip()
            if section and section not in chapter_sections[chapter]:
                chapter_sections[chapter].append(section)

        return [
            {
                "chapter": chapter,
                "sections": sorted(sections),
            }
            for chapter, sections in sorted(chapter_sections.items(), key=lambda item: item[0])
        ]

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
        structure_by_subject: dict[str, list[dict[str, str | None]]] = {}
        seen_structure_entries: dict[str, set[tuple[str, str, str]]] = {}
        for tags_json, subject, chapter, section in document_rows:
            if not subject:
                continue
            by_subject.setdefault(subject, set())
            structure_by_subject.setdefault(subject, [])
            seen_structure_entries.setdefault(subject, set())
            for candidate in [
                *(tags_json or []),
                chapter,
                section,
            ]:
                normalized = self._normalize_tag_candidate(candidate)
                if normalized:
                    by_subject[subject].add(normalized)
            self._append_structure_entry(
                structure_by_subject[subject],
                seen_structure_entries[subject],
                chapter=chapter,
                section=None,
                label=chapter,
                kind="chapter",
            )
            self._append_structure_entry(
                structure_by_subject[subject],
                seen_structure_entries[subject],
                chapter=chapter,
                section=section,
                label=section,
                kind="section",
            )

        for metadata_json, subject in chunk_rows:
            if not subject or not metadata_json:
                continue
            by_subject.setdefault(subject, set())
            structure_by_subject.setdefault(subject, [])
            seen_structure_entries.setdefault(subject, set())
            for candidate in [
                metadata_json.get("chapter"),
                metadata_json.get("section"),
            ]:
                normalized = self._normalize_tag_candidate(candidate)
                if normalized:
                    by_subject[subject].add(normalized)
            self._append_structure_entry(
                structure_by_subject[subject],
                seen_structure_entries[subject],
                chapter=metadata_json.get("chapter"),
                section=None,
                label=metadata_json.get("chapter"),
                kind="chapter",
            )
            self._append_structure_entry(
                structure_by_subject[subject],
                seen_structure_entries[subject],
                chapter=metadata_json.get("chapter"),
                section=metadata_json.get("section"),
                label=metadata_json.get("section"),
                kind="section",
            )

        self._cache = {}
        self._structure_cache = {}
        for subject, tags in by_subject.items():
            self._cache[subject] = sorted(tags, key=len, reverse=True)
        for subject, entries in structure_by_subject.items():
            self._structure_cache[subject] = sorted(
                entries,
                key=lambda item: (
                    str(item.get("kind") or "") != "section",
                    -len(str(item.get("normalized") or "")),
                    str(item.get("chapter") or ""),
                    str(item.get("section") or ""),
                ),
            )

        self._cache_time = time.time()
        total = sum(len(v) for v in self._cache.values())
        logger.info(
            "Auto-tag vocabulary refreshed: %d subjects, %d total tags",
            len(self._cache),
            total,
        )

    def _append_structure_entry(
        self,
        entries: list[dict[str, str | None]],
        seen: set[tuple[str, str, str]],
        *,
        chapter: str | None,
        section: str | None,
        label: str | None,
        kind: str,
    ) -> None:
        normalized_label = self._normalize_tag_candidate(label)
        normalized_chapter = self._normalize_tag_candidate(chapter)
        if not normalized_label or not normalized_chapter:
            return
        if kind == "section" and not self._normalize_tag_candidate(section):
            return
        key = (kind, normalized_chapter, normalized_label)
        if key in seen:
            return
        seen.add(key)
        entries.append(
            {
                "kind": kind,
                "chapter": str(chapter).strip() if str(chapter or "").strip() else None,
                "section": str(section).strip() if str(section or "").strip() else None,
                "label": str(label).strip() if str(label or "").strip() else None,
                "normalized": normalized_label,
                "match_key": self._normalize_structure_match_key(label),
                "chapter_number": self._extract_chapter_number(chapter),
                "section_number": self._extract_section_number(section),
                "section_style": self._extract_section_style(section),
            }
        )

    def _structure_match_score(self, title_key: str, label_key: str, *, kind: str) -> int | None:
        if not title_key or not label_key:
            return None
        exact_bonus = 40 if kind == "section" else 0
        partial_bonus = 15 if kind == "chapter" else 0
        if title_key == label_key:
            return 300 + exact_bonus
        if label_key in title_key:
            return 220 + partial_bonus
        if title_key in label_key and len(title_key) >= _MIN_TAG_LENGTH + 1:
            return 180 + partial_bonus
        if self._unordered_text_equivalent(title_key, label_key):
            return 200 + exact_bonus
        min_length = min(len(title_key), len(label_key))
        if min_length < _MIN_TAG_LENGTH + 1:
            return None
        sequence_ratio = SequenceMatcher(None, title_key, label_key).ratio()
        if sequence_ratio >= 0.82:
            return int(170 + sequence_ratio * 40) + exact_bonus
        return None

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

    def _strip_curriculum_prefixes(self, value: str) -> str:
        normalized = str(value or "").strip()
        while normalized:
            next_value = normalized
            for pattern in _CURRICULUM_PREFIX_PATTERNS:
                next_value = pattern.sub("", next_value, count=1).strip()
            if next_value == normalized:
                return normalized
            normalized = next_value
        return normalized

    def _strip_trailing_curriculum_suffix(self, value: str) -> str:
        normalized = str(value or "").strip()
        while normalized:
            next_value = normalized
            for pattern in _TRAILING_CURRICULUM_SUFFIX_PATTERNS:
                next_value = pattern.sub("", next_value, count=1).strip()
            if next_value == normalized:
                return normalized
            normalized = next_value
        return normalized

    def _strip_outline_prefixes(self, value: str) -> str:
        normalized = str(value or "").strip()
        while normalized:
            next_value = normalized
            for pattern in _OUTLINE_PREFIX_PATTERNS:
                next_value = pattern.sub("", next_value, count=1).strip()
            if next_value == normalized:
                return normalized
            normalized = next_value
        return normalized

    def _structure_title_candidates(self, title: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add(candidate: str | None) -> None:
            normalized = str(candidate or "").strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(normalized)

        add(title)
        stripped_title = self._strip_curriculum_prefixes(title)
        add(stripped_title)
        stripped_suffix_title = self._strip_trailing_curriculum_suffix(stripped_title)
        add(stripped_suffix_title)
        add(self._strip_outline_prefixes(stripped_suffix_title))
        for base in list(candidates):
            for pattern in _STRUCTURE_TAIL_PATTERNS:
                matched = pattern.search(base)
                if matched:
                    add(matched.group(1))
                    add(self._strip_outline_prefixes(matched.group(1)))

        keys: list[str] = []
        key_seen: set[str] = set()
        for candidate in candidates:
            key = self._normalize_structure_match_key(candidate)
            if not key or key in key_seen:
                continue
            key_seen.add(key)
            keys.append(key)
        return keys

    def _normalize_structure_match_key(self, value: str | None) -> str:
        normalized = self._normalize_title_tag_candidate(value) or self._normalize_tag_candidate(value) or str(value or "").strip()
        normalized = self._strip_curriculum_prefixes(normalized)
        normalized = self._strip_trailing_curriculum_suffix(normalized)
        normalized = self._strip_outline_prefixes(normalized)
        for pattern in _TAG_PREFIX_PATTERNS:
            normalized = pattern.sub("", normalized).strip()
        normalized = _FUZZY_SEPARATOR_PATTERN.sub("", normalized)
        normalized = _FUZZY_CONNECTOR_PATTERN.sub("", normalized)
        return normalized.lower()

    def _unordered_text_equivalent(self, title_key: str, label_key: str) -> bool:
        if len(title_key) < _MIN_TAG_LENGTH + 1 or len(label_key) < _MIN_TAG_LENGTH + 1:
            return False
        return Counter(title_key) == Counter(label_key)

    def _extract_structure_number_hints(self, title: str) -> tuple[int | None, int | None, bool]:
        normalized = str(title or "").strip()
        decimal_match = _LEADING_DECIMAL_OUTLINE_PATTERN.match(normalized)
        if decimal_match:
            return int(decimal_match.group(1)), int(decimal_match.group(2)), True
        chapter_match = _CHAPTER_NUMBER_PATTERN.search(normalized)
        section_match = _SECTION_NUMBER_PATTERN.search(normalized)
        return (
            self._parse_mixed_number(chapter_match.group(1)) if chapter_match else None,
            self._parse_mixed_number(section_match.group(1)) if section_match else None,
            False,
        )

    def _extract_chapter_number(self, value: str | None) -> int | None:
        matched = _CHAPTER_NUMBER_PATTERN.search(str(value or ""))
        if not matched:
            return None
        return self._parse_mixed_number(matched.group(1))

    def _extract_section_number(self, value: str | None) -> int | None:
        normalized = str(value or "").strip()
        decimal_match = _DECIMAL_SECTION_NUMBER_PATTERN.match(normalized)
        if decimal_match:
            return int(decimal_match.group(1))
        section_match = _SECTION_NUMBER_PATTERN.search(normalized)
        if section_match:
            return self._parse_mixed_number(section_match.group(1))
        return None

    def _extract_section_style(self, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if _DECIMAL_SECTION_NUMBER_PATTERN.match(normalized):
            return "decimal"
        if _SECTION_NUMBER_PATTERN.search(normalized):
            return "ordinal"
        return None

    def _parse_mixed_number(self, value: str | None) -> int | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if normalized.isdigit():
            return int(normalized)
        if normalized == "十":
            return 10
        total = 0
        if "十" in normalized:
            left, _, right = normalized.partition("十")
            total += (self._parse_single_chinese_digit(left) or 1) * 10
            if right:
                total += self._parse_single_chinese_digit(right) or 0
            return total or None
        return self._parse_single_chinese_digit(normalized)

    def _parse_single_chinese_digit(self, value: str | None) -> int | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if len(normalized) == 1 and normalized in _CHINESE_DIGITS:
            return _CHINESE_DIGITS[normalized]
        return None

    def _fallback_match_by_chapter_number(
        self,
        *,
        subject_entries: list[dict[str, str | None]],
        chapter_hint: int | None,
        prefers_decimal_sections: bool,
    ) -> dict[str, str | None] | None:
        if chapter_hint is None:
            return None

        chapter_entries = [
            entry
            for entry in subject_entries
            if str(entry.get("kind") or "") == "chapter"
            and self._as_int(entry.get("chapter_number")) == chapter_hint
        ]
        if not chapter_entries:
            return None

        if prefers_decimal_sections:
            decimal_chapters = {
                str(entry.get("chapter") or "")
                for entry in subject_entries
                if self._as_int(entry.get("chapter_number")) == chapter_hint
                and entry.get("section_style") == "decimal"
            }
            filtered = [
                entry
                for entry in chapter_entries
                if str(entry.get("chapter") or "") in decimal_chapters
            ]
            if len(filtered) == 1:
                return filtered[0]
            if filtered:
                chapter_entries = filtered

        return chapter_entries[0]

    def _as_int(self, value: object) -> int | None:
        return int(value) if isinstance(value, int) else None

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
