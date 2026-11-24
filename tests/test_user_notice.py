from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import backend.database as backend_database
from backend.database import Base, get_db
from backend.models import audit_log, user  # noqa: F401
from backend.models.audit_log import AuditLog
from backend.models.user import User, UserRole
from backend.routers import auth as auth_router
from backend.services.auth_service import auth_service
from backend.services.store_service import MemoryStore
from backend.user_notice import CURRENT_USER_NOTICE_VERSION


@pytest.fixture()
def notice_client(monkeypatch: pytest.MonkeyPatch):
    memory_store = MemoryStore()
    monkeypatch.setattr("backend.services.auth_service.store", memory_store)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    app = FastAPI()
    app.include_router(auth_router.router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield client, SessionLocal

    engine.dispose()


def _create_user(
    session: Session,
    *,
    username: str,
    accepted_version: str | None = None,
) -> tuple[User, dict[str, str]]:
    account = User(
        username=username,
        full_name=f"测试用户-{username}",
        role=UserRole.STUDENT,
        password_hash="sso-managed",
        user_notice_accepted_version=accepted_version,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    tokens = auth_service.issue_token_pair(account)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return account, headers


def test_existing_and_legacy_users_require_user_notice_acceptance(notice_client) -> None:
    client, SessionLocal = notice_client
    with SessionLocal() as session:
        _, headers_null = _create_user(session, username="legacy-null", accepted_version=None)
        _, headers_old = _create_user(session, username="legacy-v0", accepted_version="legacy-v0")

    resp_null = client.get("/api/auth/me", headers=headers_null)
    assert resp_null.status_code == 200
    body_null = resp_null.json()
    assert body_null["must_accept_user_notice"] is True
    assert body_null["current_user_notice_version"] == CURRENT_USER_NOTICE_VERSION
    assert body_null["user_notice_accepted_version"] is None

    resp_old = client.get("/api/auth/me", headers=headers_old)
    assert resp_old.status_code == 200
    body_old = resp_old.json()
    assert body_old["must_accept_user_notice"] is True
    assert body_old["user_notice_accepted_version"] == "legacy-v0"


def test_accept_user_notice_rejects_mismatched_version(notice_client) -> None:
    client, SessionLocal = notice_client
    with SessionLocal() as session:
        account, headers = _create_user(session, username="mismatch-student", accepted_version=None)
        account_id = account.id

    resp = client.post(
        "/api/auth/user-notice/accept",
        headers=headers,
        json={"version": "wrong-version"},
    )
    assert resp.status_code == 400

    with SessionLocal() as session:
        refreshed = session.get(User, account_id)
        assert refreshed is not None
        assert refreshed.user_notice_accepted_version is None
        assert refreshed.user_notice_accepted_at is None


def test_accept_user_notice_persists_state_and_audit_log(notice_client) -> None:
    client, SessionLocal = notice_client
    with SessionLocal() as session:
        account, headers = _create_user(session, username="accept-student", accepted_version=None)
        account_id = account.id

    resp = client.post(
        "/api/auth/user-notice/accept",
        headers=headers,
        json={"version": CURRENT_USER_NOTICE_VERSION},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["must_accept_user_notice"] is False
    assert payload["user_notice_accepted_version"] == CURRENT_USER_NOTICE_VERSION
    assert payload["user_notice_accepted_at"] is not None

    me_resp = client.get("/api/auth/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["must_accept_user_notice"] is False

    with SessionLocal() as session:
        refreshed = session.get(User, account_id)
        assert refreshed is not None
        assert refreshed.user_notice_accepted_version == CURRENT_USER_NOTICE_VERSION
        assert refreshed.user_notice_accepted_at is not None
        log = session.scalar(
            select(AuditLog).where(
                AuditLog.actor_id == account_id,
                AuditLog.action == "user_notice_accepted",
            )
        )
        assert log is not None
        assert log.detail.get("version") == CURRENT_USER_NOTICE_VERSION


def test_runtime_schema_updates_adds_user_notice_columns_with_null_for_existing_users(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "legacy_users.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    with test_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    full_name VARCHAR(64) NOT NULL,
                    role VARCHAR(16) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, full_name, role, password_hash) "
                "VALUES (1, 'old-user', '老用户', 'student', 'hash')"
            )
        )

    monkeypatch.setattr(backend_database, "engine", test_engine)
    backend_database.apply_runtime_schema_updates()

    columns = {column["name"] for column in inspect(test_engine).get_columns("users")}
    assert "user_notice_accepted_version" in columns
    assert "user_notice_accepted_at" in columns

    with test_engine.connect() as connection:
        row = connection.execute(
            text("SELECT user_notice_accepted_version, user_notice_accepted_at FROM users WHERE id = 1")
        ).one()
        assert row[0] is None
        assert row[1] is None

    test_engine.dispose()
