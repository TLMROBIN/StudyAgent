from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SqlEnum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    return [str(item.value) for item in enum_cls]


class AccountBillingType(str, Enum):
    TOKEN_PLAN = "token_plan"
    PAY_AS_YOU_GO = "pay_as_you_go"
    LOCAL = "local"


class LLMProviderAccount(TimestampMixin, Base):
    __tablename__ = "llm_provider_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(512))
    account_billing_type: Mapped[AccountBillingType] = mapped_column(
        SqlEnum(AccountBillingType, values_callable=_enum_values),
        default=AccountBillingType.PAY_AS_YOU_GO,
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship(back_populates="created_llm_provider_accounts")
    model_configs: Mapped[list["LLMModelConfig"]] = relationship(back_populates="provider_account")


if TYPE_CHECKING:
    from backend.models.llm_model import LLMModelConfig
    from backend.models.user import User
