from __future__ import annotations

from typing import Any, TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class LLMUsageEvent(TimestampMixin, Base):
    __tablename__ = "llm_usage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    model_config_id: Mapped[int] = mapped_column(ForeignKey("llm_model_configs.id", ondelete="CASCADE"))
    provider_account_id: Mapped[int] = mapped_column(ForeignKey("llm_provider_accounts.id", ondelete="CASCADE"))
    model_key: Mapped[str] = mapped_column(String(64))
    provider_name: Mapped[str] = mapped_column(String(64))
    provider_model: Mapped[str] = mapped_column(String(128))
    billing_mode: Mapped[str] = mapped_column(String(32))
    actual_model_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_provider_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prompt_cache_hit_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prompt_cache_miss_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="local_estimate")
    policy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reservation_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)

    model_config: Mapped["LLMModelConfig"] = relationship(back_populates="usage_events")
    provider_account: Mapped["LLMProviderAccount"] = relationship()
    user: Mapped["User"] = relationship()


if TYPE_CHECKING:
    from backend.models.llm_account import LLMProviderAccount
    from backend.models.llm_model import LLMModelConfig
    from backend.models.user import User
