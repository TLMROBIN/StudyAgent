from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class GuidanceStage(str, Enum):
    INITIAL = "initial_guidance"
    HINT = "scaffold_hint"
    FALLBACK = "fallback_walkthrough"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


TOPIC_MAX_LENGTH = 22
IMAGE_ONLY_MESSAGE_PLACEHOLDER = "[图片提问]"
TOPIC_PREFIXES = [
    "请围绕下面这道题继续引导我，不要直接给答案：",
    "请围绕下面这道题继续引导我，不要直接给答案:",
    "请围绕下面这道题继续引导我。注意：题图我会自己看，你先基于题干文字帮助我梳理思路：",
    "请围绕下面这道题继续引导我。注意:题图我会自己看，你先基于题干文字帮助我梳理思路:",
    "请帮我分析：",
    "请帮我分析:",
    "请帮我解答：",
    "请帮我解答:",
    "请帮我讲解：",
    "请帮我讲解:",
]


def summarize_conversation_topic(subject: str, content: str | None) -> str:
    normalized = normalize_conversation_seed(content)
    if not normalized:
        if re.sub(r"\s+", " ", (content or "").strip()) == IMAGE_ONLY_MESSAGE_PLACEHOLDER:
            return f"{subject}图片答疑"
        return f"{subject}答疑"
    for prefix in TOPIC_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break
    if len(normalized) > TOPIC_MAX_LENGTH:
        return f"{normalized[:TOPIC_MAX_LENGTH].rstrip()}..."
    return normalized


def normalize_conversation_seed(content: str | None) -> str:
    normalized = re.sub(r"\s+", " ", (content or "").strip())
    if not normalized or normalized == IMAGE_ONLY_MESSAGE_PLACEHOLDER:
        return ""
    for prefix in TOPIC_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break
    return normalized.strip("：:；;，,。！？!?、 ")


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    guidance_stage: Mapped[GuidanceStage] = mapped_column(SqlEnum(GuidanceStage), default=GuidanceStage.INITIAL)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    deleted_by_student_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    student: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )

    @property
    def topic(self) -> str:
        for message in self.messages:
            if message.role == MessageRole.USER and message.content.strip():
                return summarize_conversation_topic(self.subject, message.content)
        return f"{self.subject}答疑"


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[MessageRole] = mapped_column(SqlEnum(MessageRole), index=True)
    content: Mapped[str] = mapped_column(Text)
    turn_index: Mapped[int] = mapped_column(Integer, default=0)
    guidance_stage: Mapped[GuidanceStage] = mapped_column(SqlEnum(GuidanceStage), default=GuidanceStage.INITIAL)
    llm_model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    attachment: Mapped["ChatMessageAttachment | None"] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ChatMessageAttachment(TimestampMixin, Base):
    __tablename__ = "chat_message_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        index=True,
        unique=True,
    )
    owner_student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    storage_key: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    ocr_status: Mapped[str] = mapped_column(String(32), default="pending")
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    message: Mapped[Message] = relationship(back_populates="attachment")

    @property
    def asset_id(self) -> str:
        return f"chat-attachment-{self.id}"

    @property
    def attachment_id(self) -> str:
        return self.asset_id

    @property
    def filename(self) -> str:
        return self.original_filename

    @property
    def content_type(self) -> str:
        return self.mime_type

    @property
    def url(self) -> str:
        return f"/api/chat/attachments/{self.id}"


@event.listens_for(ChatMessageAttachment, "after_delete")
def _cleanup_chat_attachment_file(_mapper, _connection, target: ChatMessageAttachment) -> None:
    from backend.services.chat_attachment_service import chat_attachment_service

    chat_attachment_service.delete(target.storage_key)


if TYPE_CHECKING:
    from backend.models.user import User
