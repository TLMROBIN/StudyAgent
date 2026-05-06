from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class LLMProviderConfig(TimestampMixin, Base):
    __tablename__ = "llm_provider_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(512))
    model: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship(back_populates="created_llm_provider_configs")


if TYPE_CHECKING:
    from backend.models.user import User
