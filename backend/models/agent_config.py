from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class AgentConfig(TimestampMixin, Base):
    __tablename__ = "agent_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    system_prompt: Mapped[str] = mapped_column(Text)
    guidance_params: Mapped[dict] = mapped_column(JSON, default=dict)
    subject_prompts: Mapped[dict] = mapped_column(JSON, default=dict)
    filter_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship(back_populates="created_agent_configs")


if TYPE_CHECKING:
    from backend.models.user import User
