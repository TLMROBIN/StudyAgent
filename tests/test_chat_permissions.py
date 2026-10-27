from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models.audit_log import AuditLog
from backend.models.user import User, UserRole
from backend.routers import admin, auth, chat, notifications
from backend.services.chat_permission_service import ensure_chat_allowed, format_chat_ban_message


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory() as db:
        users = [User(username=role, full_name=role, role=UserRole(role), password_hash="hash")
                 for role in ("admin", "teacher", "student")]
        db.add_all(users)
        db.commit()
        ids = {u.role.value: u.id for u in users}
    active = {"id": ids["admin"]}
    app = FastAPI()
    for router in (admin.router, auth.router, chat.router, notifications.router):
        app.include_router(router)

    def db_dependency():
        with factory() as db:
            yield db

    def user_dependency():
        with factory() as db:
            yield db.get(User, active["id"])

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_current_user] = user_dependency
    with TestClient(app) as client:
        yield client, factory, active, ids
    engine.dispose()


def ban(client, sid, **changes):
    payload = {"until": (datetime.now(UTC) + timedelta(days=2)).isoformat(), "reason": "问答用途管理"}
    payload.update(changes)
    return client.put(f"/api/admin/users/{sid}/chat-ban", json=payload)


def test_ban_blocks_existing_session_before_body_parsing_and_keeps_read_access(env, monkeypatch):
    client, factory, active, ids = env
    response = ban(client, ids["student"])
    assert response.status_code == 200
    assert response.json()["chat_banned"] is True
    active["id"] = ids["student"]

    async def fail_if_parsed(_request):
        raise AssertionError("banned requests must not parse uploads or invoke models")

    monkeypatch.setattr(chat, "_parse_stream_request_payload", fail_if_parsed)
    result = client.post("/api/chat/stream", content="invalid payload")
    assert result.status_code == 403
    assert "北京时间" in result.json()["detail"]
    assert client.post("/api/chat/recommendations", json={"subject": "数学", "question": "函数单调性"}).status_code == 403
    profile = client.get("/api/auth/me").json()
    assert profile["chat_banned"] is True
    assert profile["chat_ban_message"] == result.json()["detail"]
    assert "意见反馈" in profile["chat_ban_message"] and "申诉" in profile["chat_ban_message"]
    assert "管理员不一定有时间查看或回复" in profile["chat_ban_message"]
    assert "您" not in profile["chat_ban_message"]
    assert client.get("/api/chat/history").status_code == 200
    assert client.get("/api/notifications/active").status_code == 200
    with factory() as db:
        assert db.get(User, ids["student"]).is_active
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "ban_student_chat")).all()
        assert len(logs) == 1 and logs[0].actor_id == ids["admin"]


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_only_admin_may_manage_chat_permission(env, role):
    client, _, active, ids = env
    active["id"] = ids[role]
    assert ban(client, ids["student"]).status_code == 403
    assert client.delete(f'/api/admin/users/{ids["student"]}/chat-ban').status_code == 403


def test_validation_unban_and_expiry_automatically_restore_access(env):
    client, factory, active, ids = env
    assert ban(client, ids["teacher"]).status_code == 400
    assert ban(client, 999999).status_code == 404
    assert ban(client, ids["student"], until="2020-01-01T00:00:00+08:00").status_code == 400
    assert ban(client, ids["student"], until="2027-07-07T00:00:00").status_code == 422
    assert ban(client, ids["student"], reason="   ").status_code == 422
    assert ban(client, ids["student"], until="2027-07-07T00:00:00+08:00").status_code == 200
    with factory() as db:
        s = db.get(User, ids["student"])
        assert s.chat_banned_until == datetime(2027, 7, 6, 16, 0)
        with pytest.raises(HTTPException):
            ensure_chat_allowed(s)
        # SQLite returns naive datetimes; expiry must still compare in UTC.
        s.chat_banned_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()
        assert not s.chat_banned
        ensure_chat_allowed(s)
    assert ban(client, ids["student"]).status_code == 200
    response = client.delete(f'/api/admin/users/{ids["student"]}/chat-ban')
    assert response.status_code == 200
    assert response.json()["chat_banned"] is False
    assert response.json()["chat_banned_until"] is None
    assert response.json()["chat_ban_message"] is None
    with factory() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "unban_student_chat"))


def test_direct_stream_call_also_enforces_ban_before_generation(env):
    import asyncio
    from backend.models.schemas import ChatRequest
    _, factory, _, ids = env
    with factory() as db:
        student = db.get(User, ids["student"])
        student.chat_banned_until = datetime.now(UTC) + timedelta(days=1)
        with pytest.raises(HTTPException) as error:
            asyncio.run(chat.stream_chat(ChatRequest(subject="语文", message="阅读理解"), db, student, None))
        assert error.value.status_code == 403


def test_existing_database_migration_is_idempotent_and_preserves_users(monkeypatch):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    migration = importlib.import_module("backend.alembic.versions.20261027_0023_student_chat_restrictions")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, full_name VARCHAR(64))"))
        connection.execute(text("INSERT INTO users VALUES (1, '既有学生')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert {"chat_banned_until", "chat_ban_reason"} <= {c["name"] for c in inspect(connection).get_columns("users")}
        assert connection.execute(text("SELECT full_name, chat_banned_until FROM users")).one() == ("既有学生", None)
    engine.dispose()


@pytest.mark.parametrize("end, expected", [
    ("2027-07-07T00:00:00+08:00", "2027年7月6日 23:59:59（北京时间）"),
    ("2027-01-03T00:00:00+08:00", "2027年1月2日 23:59:59（北京时间）"),
    ("2027-08-01T07:30:00+08:00", "2027年8月1日 07:29:59（北京时间）"),
])
def test_student_notice_uses_actual_inclusive_deadline(end, expected):
    message = format_chat_ban_message(datetime.fromisoformat(end))
    assert message.startswith("你因违规使用答疑平台 AI 功能，已被禁止问答权限。")
    assert expected in message
    assert "意见反馈" in message and "申诉" in message
