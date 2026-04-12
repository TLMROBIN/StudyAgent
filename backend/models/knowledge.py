from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SqlEnum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class DocumentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResourceType(str, Enum):
    KNOWLEDGE_NOTE = "knowledge_note"
    TEXTBOOK = "textbook"
    EXERCISE = "exercise"
    QUESTION_SET = "question_set"
    EXTENSION = "extension"


class DifficultyLevel(str, Enum):
    BASIC = "basic"
    STANDARD = "standard"
    ADVANCED = "advanced"
    CHALLENGE = "challenge"


class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    resource_type: Mapped[str] = mapped_column(String(32), default=ResourceType.KNOWLEDGE_NOTE.value, index=True)
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    chapter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[DocumentStatus] = mapped_column(SqlEnum(DocumentStatus), default=DocumentStatus.PENDING, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship(back_populates="uploaded_documents")
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    tasks: Mapped[list["ImportTask"]] = relationship(back_populates="document", cascade="all, delete-orphan")

    @property
    def tags(self) -> list[str]:
        value = self.tags_json or []
        return [str(item).strip() for item in value if str(item).strip()]


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class ImportTask(TimestampMixin, Base):
    __tablename__ = "import_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[DocumentStatus] = mapped_column(SqlEnum(DocumentStatus), default=DocumentStatus.PENDING, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="tasks")

    @property
    def status_message(self) -> str:
        if self.error_message:
            return self.error_message
        status_messages = {
            DocumentStatus.PENDING: "任务已创建，等待处理",
            DocumentStatus.PROCESSING: "任务处理中",
            DocumentStatus.COMPLETED: "任务已完成",
            DocumentStatus.FAILED: "任务失败",
            DocumentStatus.CANCELLED: "任务已取消",
        }
        return status_messages.get(self.status, "任务状态未知")

    @property
    def document_filename(self) -> str | None:
        return self.document.filename if self.document else None

    @property
    def document_subject(self) -> str | None:
        return self.document.subject if self.document else None


if TYPE_CHECKING:
    from backend.models.user import User
