from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, DateTime, Enum as SqlEnum, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.grade_utils import format_grade_label
from backend.models.base import TimestampMixin


class UserRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


teacher_classes = Table(
    "teacher_classes",
    Base.metadata,
    Column("teacher_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("classroom_id", ForeignKey("classrooms.id", ondelete="CASCADE"), primary_key=True),
)


class Classroom(TimestampMixin, Base):
    __tablename__ = "classrooms"
    __table_args__ = (UniqueConstraint("grade", "name", name="uq_classroom_grade_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    grade: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(50), index=True)

    students: Mapped[list["User"]] = relationship(back_populates="classroom", cascade="all, delete-orphan")
    teachers: Mapped[list["User"]] = relationship(secondary=teacher_classes, back_populates="teacher_classrooms")


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    student_no: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    full_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[UserRole] = mapped_column(SqlEnum(UserRole), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_grade_promotion_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    graduated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    classroom_id: Mapped[int | None] = mapped_column(ForeignKey("classrooms.id", ondelete="SET NULL"), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    classroom: Mapped[Classroom | None] = relationship(back_populates="students")
    teacher_classrooms: Mapped[list[Classroom]] = relationship(secondary=teacher_classes, back_populates="teachers")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="student")
    uploaded_documents: Mapped[list["KnowledgeDocument"]] = relationship(back_populates="creator")
    created_agent_configs: Mapped[list["AgentConfig"]] = relationship(back_populates="creator")
    created_llm_provider_configs: Mapped[list["LLMProviderConfig"]] = relationship(back_populates="creator")

    @property
    def is_graduated(self) -> bool:
        return self.role == UserRole.STUDENT and self.graduated_at is not None and self.grade is None

    @property
    def grade_label(self) -> str | None:
        return format_grade_label(self.grade, graduated=self.is_graduated)

    @property
    def classroom_name(self) -> str | None:
        return self.classroom.name if self.classroom else None

    @property
    def classroom_label(self) -> str | None:
        if self.is_graduated:
            return "毕业"
        grade_label = self.grade_label
        if self.classroom:
            if grade_label is not None:
                return f"{grade_label}{self.classroom.name}"
            return self.classroom.name
        return grade_label


if TYPE_CHECKING:
    from backend.models.agent_config import AgentConfig
    from backend.models.conversation import Conversation
    from backend.models.knowledge import KnowledgeDocument
    from backend.models.llm_provider import LLMProviderConfig
