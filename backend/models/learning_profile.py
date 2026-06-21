from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class StudentErrorEvent(TimestampMixin, Base):
    __tablename__ = "student_error_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True)
    knowledge_point: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    error_type: Mapped[str] = mapped_column(String(64), index=True)
    evidence_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    student: Mapped["User"] = relationship()


class StudentSkillProfile(TimestampMixin, Base):
    __tablename__ = "student_skill_profiles"
    __table_args__ = (UniqueConstraint("student_id", "subject", name="uq_student_skill_profiles_student_subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)

    student: Mapped["User"] = relationship()


if TYPE_CHECKING:
    from backend.models.user import User
