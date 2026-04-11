from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from backend.time_utils import BEIJING_TZ, now_beijing

HIGH_SCHOOL_GRADE_LABELS = {
    1: "高一",
    2: "高二",
    3: "高三",
}

_GRADE_TAG_ALIASES = {
    1: {"高一", "高一年级", "一年级", "1年级", "1 年级", "高1", "高1年级"},
    2: {"高二", "高二年级", "二年级", "2年级", "2 年级", "高2", "高2年级"},
    3: {"高三", "高三年级", "三年级", "3年级", "3 年级", "高3", "高3年级"},
}


def format_grade_label(grade: int | None, *, graduated: bool = False) -> str | None:
    if graduated:
        return "毕业"
    if grade is None:
        return None
    return HIGH_SCHOOL_GRADE_LABELS.get(grade, f"{grade}年级")


def extract_grade_levels(tags: Iterable[str] | None) -> set[int]:
    if not tags:
        return set()
    detected: set[int] = set()
    normalized_tags = {
        str(tag).strip().lower().replace(" ", "")
        for tag in tags
        if str(tag).strip()
    }
    for grade, aliases in _GRADE_TAG_ALIASES.items():
        normalized_aliases = {alias.lower().replace(" ", "") for alias in aliases}
        if normalized_tags & normalized_aliases:
            detected.add(grade)
    return detected


def effective_promotion_year(now: datetime | None = None) -> int:
    current_time = now or now_beijing()
    localized = current_time.astimezone(BEIJING_TZ) if current_time.tzinfo else current_time.replace(tzinfo=BEIJING_TZ)
    return localized.year if localized.month >= 8 else localized.year - 1
