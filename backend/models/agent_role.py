from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.models.base import TimestampMixin


class AgentRole(TimestampMixin, Base):
    __tablename__ = "agent_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    subjects: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    current_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class AgentRoleRevision(TimestampMixin, Base):
    __tablename__ = "agent_role_revisions"
    __table_args__ = (UniqueConstraint("role_id", "revision", name="uq_agent_role_revisions_role_revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("agent_roles.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    style_config: Mapped[dict] = mapped_column(JSON)
    renderer_version: Mapped[str] = mapped_column(String(32))
    rendered_prompt: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
