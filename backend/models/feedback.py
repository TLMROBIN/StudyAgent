from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class StudentFeedback(TimestampMixin, Base):
    __tablename__ = "student_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    reply_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    replied_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    student: Mapped["User"] = relationship(foreign_keys=[student_id], back_populates="feedback_items")
    replier: Mapped["User | None"] = relationship(foreign_keys=[replied_by])


class StudentFeedbackBan(TimestampMixin, Base):
    __tablename__ = "student_feedback_bans"
    __table_args__ = (UniqueConstraint("student_id", name="uq_student_feedback_bans_student_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    banned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    student: Mapped["User"] = relationship(foreign_keys=[student_id], back_populates="feedback_ban")
    admin: Mapped["User | None"] = relationship(foreign_keys=[banned_by])


if TYPE_CHECKING:
    from backend.models.user import User
