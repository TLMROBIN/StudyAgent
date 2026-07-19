"""系统参数配置服务：DB → 环境变量 → 默认值 三级读取。

- 白名单（CONFIG_ITEMS）限定可通过 admin API 读写的 key，杜绝任意键写入。
- is_secret 项在 DB 中存 Fernet 密文（密钥由 settings.jwt_secret_key 经
  SHA-256 + urlsafe base64 派生，cryptography 已在依赖中），读出时解密；
  对外接口仅返回掩码（保留后 4 位）。
- 进程内缓存（dict + 写入时失效），避免每次解析 PDF 都查库。
- DB 不可用（表不存在、连接失败等）时优雅降级到 env/default，不炸调用方。
"""

from __future__ import annotations

import base64
from hashlib import sha256
import os
import threading
from typing import Any

from cryptography.fernet import Fernet

from backend.config import get_settings

# ---------------------------------------------------------------------------
# 可配置项白名单（key、类型、是否 secret、说明、默认值）
# env 变量名与 key 相同（与 config.py 中 Settings 字段 alias 一致）。
# ---------------------------------------------------------------------------
CONFIG_ITEMS: list[dict[str, Any]] = [
    {
        "key": "PDF_PARSER_BACKEND",
        "type": "enum",
        "choices": ["auto", "legacy", "mineru", "mineru_remote"],
        "secret": False,
        "default": "auto",
        "description": "PDF 解析后端：auto=本地优先自动降级（推荐）；mineru=仅本地；mineru_remote=仅远程API；legacy=传统解析",
    },
    {
        "key": "MINERU_REMOTE_API_KEY",
        "type": "string",
        "secret": True,
        "default": "",
        "description": "MinerU 官方云 API Token（https://mineru.net），留空表示未配置",
    },
    {
        "key": "MINERU_REMOTE_302_API_KEY",
        "type": "string",
        "secret": True,
        "default": "",
        "description": "302.ai API Key（用于 302_free / 302_paid 降级链路），留空表示未配置",
    },
    {
        "key": "MINERU_REMOTE_PROVIDERS",
        "type": "string",
        "secret": False,
        "default": "official,302_free,302_paid",
        "description": "远程解析 provider 降级链，逗号分隔（official,302_free,302_paid）",
    },
    {
        "key": "MINERU_REMOTE_MODEL_VERSION",
        "type": "string",
        "secret": False,
        "default": "vlm",
        "description": "远程 MinerU 模型版本（如 vlm / pipeline）",
    },
    {
        "key": "MINERU_REMOTE_PUBLIC_BASE_URL",
        "type": "string",
        "secret": False,
        "default": "",
        "description": "公网可达的文件 URL 前缀（302 系 provider 只接受 URL 拉取，不接受上传）",
    },
    {
        "key": "MINERU_REMOTE_POLL_INTERVAL_SECONDS",
        "type": "int",
        "secret": False,
        "default": "5",
        "description": "远程任务轮询间隔（秒）",
    },
    {
        "key": "MINERU_REMOTE_TIMEOUT_SECONDS",
        "type": "int",
        "secret": False,
        "default": "600",
        "description": "远程任务整体超时（秒）",
    },
]

_ITEM_BY_KEY = {item["key"]: item for item in CONFIG_ITEMS}
_FERNET_PREFIX = "fernet:"


def _fernet() -> Fernet:
    digest = sha256(get_settings().jwt_secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    return _FERNET_PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(stored: str) -> str:
    if stored.startswith(_FERNET_PREFIX):
        return _fernet().decrypt(stored[len(_FERNET_PREFIX):].encode("ascii")).decode("utf-8")
    # 历史明文/异常数据兜底：按原样返回，避免读炸。
    return stored


def mask_value(value: str) -> str:
    """密钥掩码：保留前 2 位与后 4 位，例如 sk-****-abcd。"""
    if not value:
        return ""
    if len(value) <= 6:
        return "****"
    return f"{value[:2]}****{value[-4:]}"


class SystemConfigService:
    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get_value(self, key: str, default: Any = None) -> Any:
        """优先级：DB → os.environ → default。DB 异常时静默降级。"""
        with self._lock:
            if key in self._cache:
                cached = self._cache[key]
                return default if cached is None else cached
        value = self._read_db_value(key)
        if value is None:
            env_value = os.environ.get(key)
            value = env_value if env_value is not None else None
        with self._lock:
            self._cache[key] = value
        return default if value is None else value

    def get_many(self, keys: list[str], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
        defaults = defaults or {}
        return {key: self.get_value(key, defaults.get(key)) for key in keys}

    def get_masked(self, key: str) -> str:
        """secret 项掩码；非 secret 项也可调用（返回原值的掩码形式仅用于展示对称）。"""
        value = self.get_value(key, "")
        return mask_value(str(value)) if value else ""

    def describe_items(self) -> list[dict[str, Any]]:
        """GET /api/admin/system-config 的返回体：元数据 + 当前值（secret 掩码）+ 来源。"""
        result: list[dict[str, Any]] = []
        for item in CONFIG_ITEMS:
            key = item["key"]
            db_value = self._read_db_value(key)
            env_value = os.environ.get(key)
            if db_value is not None:
                source, raw = "db", db_value
            elif env_value is not None:
                source, raw = "env", env_value
            else:
                source, raw = "default", str(item.get("default", ""))
            has_value = bool(str(raw or "").strip())
            result.append(
                {
                    "key": key,
                    "type": item["type"],
                    "choices": item.get("choices"),
                    "secret": item["secret"],
                    "description": item["description"],
                    "default": item.get("default", ""),
                    "source": source,
                    "has_value": has_value,
                    "value": mask_value(str(raw)) if item["secret"] else raw,
                }
            )
        return result

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def set_many(
        self,
        db,
        items: dict[str, Any],
        *,
        user_id: int | None,
        ip_address: str | None = None,
        actor=None,
    ) -> dict[str, str]:
        """写 DB + 审计。secret 项 value 为空串/None 表示“不覆盖”。

        返回 {key: "***" | 新值}（审计与响应用，绝不含 secret 明文）。
        """
        from backend.database import SessionLocal  # 延迟导入避免循环
        from backend.models.system_config import SystemConfig
        from backend.services.audit_service import audit_service

        unknown = sorted(set(items) - set(_ITEM_BY_KEY))
        if unknown:
            raise ValueError(f"Unknown system config keys: {', '.join(unknown)}")

        session = db if db is not None else SessionLocal()
        owns_session = db is None
        changed: dict[str, str] = {}
        try:
            for key, raw_value in items.items():
                meta = _ITEM_BY_KEY[key]
                normalized = self._normalize_value(meta, raw_value)
                if normalized is None:
                    continue  # secret 留空不覆盖 / None 值
                row = session.get(SystemConfig, key)
                stored = _encrypt(normalized) if meta["secret"] else normalized
                if row is None:
                    row = SystemConfig(key=key, value=stored, is_secret=meta["secret"], updated_by=user_id)
                    session.add(row)
                else:
                    row.value = stored
                    row.is_secret = meta["secret"]
                    row.updated_by = user_id
                session.commit()
                changed[key] = "***" if meta["secret"] else normalized
            if changed:
                audit_service.log(
                    session,
                    actor=actor,
                    action="update_system_config",
                    target_type="system_config",
                    target_id=None,
                    result="success",
                    ip_address=ip_address,
                    detail={"changed": changed},
                )
        finally:
            if owns_session:
                session.close()
        with self._lock:
            for key in items:
                self._cache.pop(key, None)
        return changed

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _normalize_value(self, meta: dict[str, Any], raw_value: Any) -> str | None:
        if raw_value is None:
            return None
        value = str(raw_value).strip()
        if meta["secret"] and value == "":
            return None  # secret 留空 = 不覆盖
        if meta["type"] == "int":
            try:
                return str(int(value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{meta['key']} must be an integer") from exc
        if meta["type"] == "enum":
            if value not in meta["choices"]:
                raise ValueError(f"{meta['key']} must be one of {meta['choices']}")
        return value

    def _read_db_value(self, key: str) -> str | None:
        """DB 读取（含 secret 解密）；任何异常都返回 None 走 env/default 降级。"""
        try:
            from backend.database import SessionLocal
            from backend.models.system_config import SystemConfig

            session = SessionLocal()
            try:
                row = session.get(SystemConfig, key)
                if row is None:
                    return None
                return _decrypt(row.value) if row.is_secret else row.value
            finally:
                session.close()
        except Exception:
            return None

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


system_config_service = SystemConfigService()
