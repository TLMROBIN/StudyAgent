from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SqlEnum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin
from backend.models.llm_account import _enum_values


class QuotaBillingMode(str, Enum):
    REQUEST_COUNT = "request_count"
    TOKEN_USAGE = "token_usage"
    FREE_LOCAL = "free_local"


class LLMModelConfig(TimestampMixin, Base):
    __tablename__ = "llm_model_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(255), default="")
    provider_account_id: Mapped[int] = mapped_column(ForeignKey("llm_provider_accounts.id", ondelete="CASCADE"))
    provider_model: Mapped[str] = mapped_column(String(128))
    capability_text: Mapped[bool] = mapped_column(Boolean, default=True)
    capability_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship(back_populates="created_llm_model_configs")
    provider_account: Mapped["LLMProviderAccount"] = relationship(back_populates="model_configs")
    quota_policy: Mapped["LLMQuotaPolicy | None"] = relationship(
        back_populates="model_config",
        cascade="all, delete-orphan",
        uselist=False,
    )
    usage_events: Mapped[list["LLMUsageEvent"]] = relationship(back_populates="model_config")


class LLMQuotaPolicy(TimestampMixin, Base):
    __tablename__ = "llm_quota_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_config_id: Mapped[int] = mapped_column(
        ForeignKey("llm_model_configs.id", ondelete="CASCADE"),
        unique=True,
    )
    billing_mode: Mapped[QuotaBillingMode] = mapped_column(
        SqlEnum(QuotaBillingMode, values_callable=_enum_values),
        default=QuotaBillingMode.REQUEST_COUNT,
    )
    user_daily_request_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    school_daily_request_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    school_daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_rolling_5h_request_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_weekly_request_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count_cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    fail_closed_on_store_error: Mapped[bool] = mapped_column(Boolean, default=True)

    model_config: Mapped["LLMModelConfig"] = relationship(back_populates="quota_policy")


if TYPE_CHECKING:
    from backend.models.llm_account import LLMProviderAccount
    from backend.models.llm_usage import LLMUsageEvent
    from backend.models.user import User
