from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_beijing() -> datetime:
    return now_utc().astimezone(BEIJING_TZ)


def assume_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_beijing(value: datetime) -> datetime:
    return assume_utc(value).astimezone(BEIJING_TZ)


def serialize_datetime_for_api(value: datetime, *, timespec: str = "auto") -> str:
    return to_beijing(value).isoformat(timespec=timespec)
