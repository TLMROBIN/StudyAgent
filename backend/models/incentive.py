from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class StudentIncentiveEvent(TimestampMixin, Base):
    __tablename__ = "student_incentive_events"
    __table_args__ = (
        Index("ix_student_incentive_events_student_created", "student_id", "created_at"),
        Index("ix_student_incentive_events_student_type", "student_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    student: Mapped["User"] = relationship(foreign_keys=[student_id])
    creator: Mapped["User | None"] = relationship(foreign_keys=[created_by])


class StudentIncentiveProfile(TimestampMixin, Base):
    __tablename__ = "student_incentive_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    current_streak_days: Mapped[int] = mapped_column(Integer, default=0)
    longest_streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_valid_learning_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    daily_points: Mapped[int] = mapped_column(Integer, default=0)
    daily_points_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    badges: Mapped[list[str]] = mapped_column(JSON, default=list)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_praise_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    student: Mapped["User"] = relationship()


if TYPE_CHECKING:
    from backend.models.user import User
