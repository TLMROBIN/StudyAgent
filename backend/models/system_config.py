from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.models.base import TimestampMixin


class SystemConfig(TimestampMixin, Base):
    """管理员可在 UI 维护的系统参数（读取优先级：DB → 环境变量 → 默认值）。

    is_secret=True 的项在库中存 Fernet 密文（密钥由 JWT_SECRET_KEY 派生），
    接口返回时只给掩码，审计日志不记录明文。
    """

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    updater: Mapped["User | None"] = relationship()


if TYPE_CHECKING:
    from backend.models.user import User
