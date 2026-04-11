from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from backend.models.user import UserRole

_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "unihan_mandarin_map.json"
_ASCII_PATTERN = re.compile(r"[a-z0-9]+")
_CLASS_DIGIT_PATTERN = re.compile(r"\d+")
_COMPOUND_SURNAMES = {
    "欧阳": "ouyang",
    "司马": "sima",
    "上官": "shangguan",
    "夏侯": "xiahou",
    "诸葛": "zhuge",
    "东方": "dongfang",
    "皇甫": "huangfu",
    "尉迟": "yuchi",
    "公孙": "gongsun",
    "长孙": "zhangsun",
}
_POLYPHONIC_OVERRIDES = {
    "单": "shan",
    "区": "ou",
    "仇": "qiu",
    "查": "zha",
    "曾": "zeng",
    "解": "xie",
    "朴": "piao",
    "乐": "yue",
    "缪": "miao",
    "沈": "shen",
    "翟": "zhai",
    "秘": "bi",
    "冼": "xian",
}
_CHINESE_NUMERAL_VALUES = {
    "零": 0,
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


@lru_cache(maxsize=1)
def _mandarin_map() -> dict[str, str]:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def _ascii_syllable(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^a-z]", "", normalized.lower())


def transliterate_name_to_pinyin(full_name: str) -> str:
    normalized = (full_name or "").strip()
    if not normalized:
        return "user"

    parts: list[str] = []
    remaining = normalized
    for surname, pinyin in _COMPOUND_SURNAMES.items():
        if remaining.startswith(surname):
            parts.append(pinyin)
            remaining = remaining[len(surname) :]
            break

    mapping = _mandarin_map()
    for character in remaining:
        if character.isspace():
            continue
        lowered = character.lower()
        if lowered.isascii() and lowered.isalnum():
            parts.append(lowered)
            continue
        if character in _POLYPHONIC_OVERRIDES:
            parts.append(_POLYPHONIC_OVERRIDES[character])
            continue
        if character in mapping:
            parts.append(_ascii_syllable(mapping[character]))
            continue
        ascii_chunks = _ASCII_PATTERN.findall(lowered)
        if ascii_chunks:
            parts.extend(ascii_chunks)
            continue
        if ord(character) > 127:
            parts.append(f"u{ord(character):x}")
    return "".join(parts) or "user"


def build_generated_username(full_name: str, role: UserRole, classroom_name: str | None = None) -> str:
    base = transliterate_name_to_pinyin(full_name)
    if role == UserRole.STUDENT:
        return f"{base}{extract_classroom_number(classroom_name)}"
    return base


def build_default_password(full_name: str) -> str:
    return f"{transliterate_name_to_pinyin(full_name)}123456"


def extract_classroom_number(classroom_name: str | None) -> str:
    normalized = (classroom_name or "").strip()
    if not normalized:
        raise ValueError("Classroom name is required")
    digit_match = _CLASS_DIGIT_PATTERN.search(normalized)
    if digit_match:
        return digit_match.group(0)
    chinese_digits = normalized.replace("班", "").strip()
    number = _parse_chinese_number(chinese_digits)
    if number is not None:
        return str(number)
    raise ValueError("Classroom name must contain a class number")


def _parse_chinese_number(text: str) -> int | None:
    if not text:
        return None
    if text == "十":
        return 10
    if "十" not in text:
        digits = [_CHINESE_NUMERAL_VALUES.get(char) for char in text]
        if any(value is None for value in digits):
            return None
        return int("".join(str(value) for value in digits))
    left, _, right = text.partition("十")
    tens = _CHINESE_NUMERAL_VALUES.get(left, 1 if not left else None)
    ones = _CHINESE_NUMERAL_VALUES.get(right, 0 if not right else None)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones
