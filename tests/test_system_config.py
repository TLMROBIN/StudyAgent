"""系统参数（管理员 UI 配置）回归测试。

覆盖：DB/env/default 优先级、secret 加密存储、掩码格式、非 admin 403、
白名单外 key 拒绝、审计日志无明文、mineru_remote_service 走 DB 配置路径。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models import agent_config, audit_log, conversation, knowledge, system_config, user  # noqa: F401
from backend.models.audit_log import AuditLog
from backend.models.system_config import SystemConfig
from backend.models.user import User, UserRole
from backend.routers import admin as admin_router
from backend.services.mineru_service import MineruStartupError
from backend.services.mineru_remote_service import MineruRemoteService
from backend.services.system_config_service import (
    SystemConfigService,
    mask_value,
    system_config_service,
)
from backend.config import Settings


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def build_admin_user(role: UserRole = UserRole.ADMIN) -> User:
    return User(id=1, username="admin", full_name="Admin", role=role, password_hash="x", is_active=True)


def build_admin_client(session_factory, current_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router.router)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def override_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_service(monkeypatch, tmp_path):
    """隔离：每个用例使用独立内存 DB 与全新 service 实例，避免污染全局缓存。"""
    SessionLocal = build_session()
    service = SystemConfigService()
    # service 内部延迟导入 SessionLocal（backend.database.SessionLocal），指向内存库。
    monkeypatch.setattr("backend.database.SessionLocal", SessionLocal)
    monkeypatch.setattr("backend.services.system_config_service.system_config_service", service)
    # 调用方（admin router / mineru_remote_service / rag_service）持有的引用也换掉。
    monkeypatch.setattr("backend.routers.admin.system_config_service", service)
    monkeypatch.setattr("backend.services.mineru_remote_service.system_config_service", service)
    monkeypatch.setattr("backend.services.rag_service.system_config_service", service)
    return Simple(SessionLocal=SessionLocal, service=service)


class Simple:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ---------------------------------------------------------------------------
# 优先级与加密
# ---------------------------------------------------------------------------
def test_db_value_beats_env_and_default(_isolate_service, monkeypatch):
    service = _isolate_service.service
    SessionLocal = _isolate_service.SessionLocal
    monkeypatch.setenv("MINERU_REMOTE_MODEL_VERSION", "env-version")
    assert service.get_value("MINERU_REMOTE_MODEL_VERSION", "fallback-default") == "env-version"

    session = SessionLocal()
    service.set_many(session, {"MINERU_REMOTE_MODEL_VERSION": "db-version"}, user_id=1)
    assert service.get_value("MINERU_REMOTE_MODEL_VERSION", "fallback-default") == "db-version"
    session.close()


def test_env_absent_falls_back_to_default(_isolate_service, monkeypatch):
    monkeypatch.delenv("MINERU_REMOTE_TIMEOUT_SECONDS", raising=False)
    assert _isolate_service.service.get_value("MINERU_REMOTE_TIMEOUT_SECONDS", "999") == "999"


def test_secret_is_encrypted_at_rest(_isolate_service):
    service = _isolate_service.service
    SessionLocal = _isolate_service.SessionLocal
    session = SessionLocal()
    service.set_many(session, {"MINERU_REMOTE_API_KEY": "sk-live-token-1234"}, user_id=1)

    row = session.scalar(select(SystemConfig).where(SystemConfig.key == "MINERU_REMOTE_API_KEY"))
    assert row is not None
    assert row.is_secret is True
    assert "sk-live-token-1234" not in row.value
    assert row.value.startswith("fernet:")
    # 读取时解密还原
    assert service.get_value("MINERU_REMOTE_API_KEY", "") == "sk-live-token-1234"
    session.close()


def test_mask_value_format():
    assert mask_value("sk-live-token-abcd") == "sk****abcd"
    assert mask_value("abc") == "****"
    assert mask_value("") == ""


def test_secret_empty_value_does_not_overwrite(_isolate_service):
    service = _isolate_service.service
    session = _isolate_service.SessionLocal()
    service.set_many(session, {"MINERU_REMOTE_API_KEY": "sk-first-9999"}, user_id=1)
    service.set_many(session, {"MINERU_REMOTE_API_KEY": ""}, user_id=1)
    assert service.get_value("MINERU_REMOTE_API_KEY", "") == "sk-first-9999"
    session.close()


def test_unknown_key_rejected(_isolate_service):
    service = _isolate_service.service
    session = _isolate_service.SessionLocal()
    with pytest.raises(ValueError, match="Unknown system config keys"):
        service.set_many(session, {"ARBITRARY_KEY": "x"}, user_id=1)
    session.close()


def test_int_and_enum_validation(_isolate_service):
    service = _isolate_service.service
    session = _isolate_service.SessionLocal()
    with pytest.raises(ValueError, match="integer"):
        service.set_many(session, {"MINERU_REMOTE_TIMEOUT_SECONDS": "not-a-number"}, user_id=1)
    with pytest.raises(ValueError, match="one of"):
        service.set_many(session, {"PDF_PARSER_BACKEND": "bogus"}, user_id=1)
    service.set_many(session, {"MINERU_REMOTE_TIMEOUT_SECONDS": 120}, user_id=1)
    assert service.get_value("MINERU_REMOTE_TIMEOUT_SECONDS", "") == "120"
    session.close()


# ---------------------------------------------------------------------------
# admin API
# ---------------------------------------------------------------------------
def test_get_system_config_returns_metadata_and_sources(_isolate_service):
    SessionLocal = _isolate_service.SessionLocal
    client = build_admin_client(SessionLocal, build_admin_user())
    response = client.get("/api/admin/system-config")
    assert response.status_code == 200
    items = {item["key"]: item for item in response.json()["items"]}
    assert "PDF_PARSER_BACKEND" in items
    assert items["PDF_PARSER_BACKEND"]["type"] == "enum"
    assert items["PDF_PARSER_BACKEND"]["choices"] == ["auto", "legacy", "mineru", "mineru_remote"]
    assert items["PDF_PARSER_BACKEND"]["source"] in {"env", "default"}
    secret_item = items["MINERU_REMOTE_API_KEY"]
    assert secret_item["secret"] is True
    assert secret_item["has_value"] is False


def test_put_system_config_masks_secret_and_audits(_isolate_service):
    SessionLocal = _isolate_service.SessionLocal
    client = build_admin_client(SessionLocal, build_admin_user())
    response = client.put(
        "/api/admin/system-config",
        json={"MINERU_REMOTE_API_KEY": "sk-ui-write-7777", "MINERU_REMOTE_PROVIDERS": "official"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"]["MINERU_REMOTE_API_KEY"] == "***"
    assert "sk-ui-write-7777" not in response.text

    items = {item["key"]: item for item in body["items"]}
    assert items["MINERU_REMOTE_API_KEY"]["source"] == "db"
    assert items["MINERU_REMOTE_API_KEY"]["has_value"] is True
    assert items["MINERU_REMOTE_API_KEY"]["value"] == "sk****7777"
    assert "sk-ui-write-7777" not in body["items"].__repr__()

    session = SessionLocal()
    entries = session.scalars(select(AuditLog).where(AuditLog.action == "update_system_config")).all()
    assert len(entries) == 1
    detail = entries[0].detail
    assert detail["changed"]["MINERU_REMOTE_API_KEY"] == "***"
    assert "sk-ui-write-7777" not in str(detail)
    session.close()


def test_put_system_config_rejects_unknown_key_and_non_admin(_isolate_service):
    SessionLocal = _isolate_service.SessionLocal
    admin_client = build_admin_client(SessionLocal, build_admin_user())
    response = admin_client.put("/api/admin/system-config", json={"NOT_A_CONFIG": "x"})
    assert response.status_code == 400

    teacher_client = build_admin_client(SessionLocal, build_admin_user(UserRole.TEACHER))
    assert teacher_client.get("/api/admin/system-config").status_code == 403
    assert teacher_client.put("/api/admin/system-config", json={}).status_code == 403


# ---------------------------------------------------------------------------
# mineru_remote_service 走 DB 配置
# ---------------------------------------------------------------------------
def test_mineru_remote_service_uses_db_config(_isolate_service, tmp_path):
    service_cfg = _isolate_service.service
    session = _isolate_service.SessionLocal()
    service_cfg.set_many(
        session,
        {
            "PDF_PARSER_BACKEND": "mineru_remote",
            "MINERU_REMOTE_API_KEY": "db-official-token",
            "MINERU_REMOTE_PROVIDERS": "official",
            "MINERU_REMOTE_POLL_INTERVAL_SECONDS": "0",
        },
        user_id=1,
    )
    session.close()

    settings = Settings(
        PDF_PARSER_BACKEND="legacy",  # settings 未启用，DB 启用 → 以 DB 为准
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        MINERU_REMOTE_API_KEY=None,
        MINERU_REMOTE_POLL_INTERVAL_SECONDS=5,
    )
    remote = MineruRemoteService(settings=settings)
    assert remote._pdf_parser_backend() == "mineru_remote"
    assert remote._configured_providers() == ["official"]
    assert remote._cfg("MINERU_REMOTE_API_KEY", None) == "db-official-token"
    assert remote._cfg_int("MINERU_REMOTE_POLL_INTERVAL_SECONDS", 5) == 0


def test_mineru_remote_service_db_backend_disables_parse(_isolate_service, tmp_path):
    service_cfg = _isolate_service.service
    session = _isolate_service.SessionLocal()
    service_cfg.set_many(session, {"PDF_PARSER_BACKEND": "legacy"}, user_id=1)
    session.close()

    settings = Settings(
        PDF_PARSER_BACKEND="mineru_remote",  # env 启用但 DB 显式关掉 → 以 DB 为准
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        MINERU_REMOTE_API_KEY="token",
    )
    remote = MineruRemoteService(settings=settings)
    source = tmp_path / "demo.pdf"
    source.write_bytes(b"%PDF-1.4")
    with pytest.raises(MineruStartupError, match="not enabled"):
        remote.parse_pdf(str(source), task_id=1, document_id=1)


def test_mineru_remote_service_graceful_without_db(monkeypatch):
    """DB 查询异常时静默降级到 env/settings（不炸）。"""
    service = SystemConfigService()

    def boom(self, key):
        raise RuntimeError("db down")

    monkeypatch.setattr(SystemConfigService, "_read_db_value", lambda self, key: None)
    monkeypatch.setattr("backend.services.mineru_remote_service.system_config_service", service)
    monkeypatch.delenv("MINERU_REMOTE_API_KEY", raising=False)

    settings = Settings(
        PDF_PARSER_BACKEND="mineru_remote",
        MINERU_REMOTE_API_KEY="env-token",
        MINERU_REMOTE_PROVIDERS="official",
    )
    remote = MineruRemoteService(settings=settings)
    assert remote._cfg("MINERU_REMOTE_API_KEY", remote.settings.mineru_remote_api_key) == "env-token"
    assert remote._configured_providers() == ["official"]
