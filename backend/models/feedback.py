from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    student: Mapped["User"] = relationship(foreign_keys=[student_id], back_populates="feedback_items")
    replier: Mapped["User | None"] = relationship(foreign_keys=[replied_by])

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


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


class StudentFeedbackReadState(TimestampMixin, Base):
    __tablename__ = "student_feedback_read_states"
    __table_args__ = (UniqueConstraint("student_id", "feedback_id", name="uq_student_feedback_read_states_student_feedback"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    feedback_id: Mapped[int] = mapped_column(ForeignKey("student_feedback.id", ondelete="CASCADE"), index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    student: Mapped["User"] = relationship(foreign_keys=[student_id])
    feedback: Mapped[StudentFeedback] = relationship()


class ReleaseNote(TimestampMixin, Base):
    __tablename__ = "release_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship(foreign_keys=[created_by])


class ReleaseNoteReadState(TimestampMixin, Base):
    __tablename__ = "release_note_read_states"
    __table_args__ = (UniqueConstraint("student_id", "release_note_id", name="uq_release_note_read_states_student_note"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    release_note_id: Mapped[int] = mapped_column(ForeignKey("release_notes.id", ondelete="CASCADE"), index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    student: Mapped["User"] = relationship(foreign_keys=[student_id])
    release_note: Mapped[ReleaseNote] = relationship()


if TYPE_CHECKING:
    from backend.models.user import User
