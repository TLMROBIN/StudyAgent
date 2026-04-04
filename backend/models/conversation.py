from __future__ import annotations

from enum import Enum
import re
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SqlEnum, ForeignKey, Integer, String, Text
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
    normalized = re.sub(r"\s+", " ", (content or "").strip())
    for prefix in TOPIC_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break
    normalized = normalized.strip("：:；;，,。！？!?、 ")
    if not normalized:
        return f"{subject}答疑"
    if len(normalized) > TOPIC_MAX_LENGTH:
        return f"{normalized[:TOPIC_MAX_LENGTH].rstrip()}..."
    return normalized


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    guidance_stage: Mapped[GuidanceStage] = mapped_column(SqlEnum(GuidanceStage), default=GuidanceStage.INITIAL)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)

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

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


if TYPE_CHECKING:
    from backend.models.user import User
