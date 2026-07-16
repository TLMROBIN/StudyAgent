from __future__ import annotations


SUBJECTS: tuple[str, ...] = (
    "语文",
    "数学",
    "英语",
    "物理",
    "化学",
    "生物",
    "政治",
    "历史",
    "地理",
)
SUBJECT_SET = frozenset(SUBJECTS)


def is_valid_subject(subject: str | None) -> bool:
    return isinstance(subject, str) and subject in SUBJECT_SET
