"""滥用防护回归：真实 IP、指标标签基数、提示词配置权限、停用吊销会话、OIDC 绑定。"""

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.middleware.rate_limit import SlidingWindowRateLimitMiddleware
from backend.middleware.real_ip import TrustedRealIPMiddleware
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.user import User, UserRole
from backend.routers import admin as admin_router
from backend.routers import agent_config as agent_config_router
from backend.services.auth_service import auth_service
from backend.services.metrics_service import render_metrics
from backend.services.oidc_service import OidcAuthError, oidc_service
from backend.services.store_service import store


def _build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def _client(router, session_factory, current_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: current_user
    return TestClient(app)


def _user(session: Session, username: str, role: UserRole) -> User:
    item = User(username=username, full_name=username, role=role, password_hash="sso-managed", is_active=True)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _echo_client_app(trusted_cidrs: list[str]) -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str]:
        return {"ip": request.client.host if request.client else ""}

    app.add_middleware(TrustedRealIPMiddleware, trusted_cidrs=trusted_cidrs)
    return app


def test_real_ip_header_is_used_only_from_trusted_proxy():
    # TestClient 的直连对端是 "testclient"（非 IP），视为不受信
    untrusted = TestClient(_echo_client_app(["172.16.0.0/12"]))
    assert untrusted.get("/whoami", headers={"X-Real-IP": "10.1.2.3"}).json()["ip"] == "testclient"

    async def call(headers: list[tuple[bytes, bytes]], peer: str) -> str:
        captured: dict[str, str] = {}

        async def inner(scope, receive, send):
            captured["ip"] = scope["client"][0]

        middleware = TrustedRealIPMiddleware(inner, trusted_cidrs=["172.16.0.0/12"])
        await middleware({"type": "http", "headers": headers, "client": (peer, 1234)}, None, None)
        return captured["ip"]

    assert asyncio.run(call([(b"x-real-ip", b"10.1.2.3")], "172.18.0.5")) == "10.1.2.3"
    assert asyncio.run(call([(b"x-real-ip", b"10.1.2.3")], "10.9.9.9")) == "10.9.9.9"
    assert asyncio.run(call([(b"x-real-ip", b"not-an-ip")], "172.18.0.5")) == "172.18.0.5"
    assert asyncio.run(call([], "172.18.0.5")) == "172.18.0.5"


def test_rate_limit_buckets_are_per_real_client_ip(monkeypatch):
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(SlidingWindowRateLimitMiddleware, limit=1, window_seconds=60)
    seen: list[str] = []

    def fake_hit(key: str, limit: int, window_seconds: int) -> bool:
        seen.append(key)
        return seen.count(key) <= limit

    monkeypatch.setattr(store, "hit_sliding_window", fake_hit)

    async def call(real_ip: str) -> int:
        status_holder: dict[str, int] = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]

        wrapped = TrustedRealIPMiddleware(app, trusted_cidrs=["172.16.0.0/12"])
        await wrapped(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/ping",
                "raw_path": b"/ping",
                "query_string": b"",
                "headers": [(b"x-real-ip", real_ip.encode())],
                "client": ("172.18.0.5", 4000),
                "server": ("backend", 8000),
                "root_path": "",
            },
            receive,
            send,
        )
        return status_holder["status"]

    assert asyncio.run(call("10.0.0.1")) == 200
    # 同一 nginx 对端后面的另一名学生不应共享同一个桶
    assert asyncio.run(call("10.0.0.2")) == 200
    assert asyncio.run(call("10.0.0.1")) == 429


def test_unmatched_paths_do_not_create_metric_labels():
    from backend.main import app

    client = TestClient(app)
    client.get("/api/random-probe-label-xyz")
    payload, _ = render_metrics()
    assert "random-probe-label-xyz" not in payload.decode("utf-8")


@pytest.mark.parametrize("role", [UserRole.STUDENT, UserRole.TEACHER])
def test_non_admin_cannot_read_agent_config(role):
    SessionLocal = _build_session()
    session = SessionLocal()
    try:
        current = _user(session, f"user-{role.value}", role)
    finally:
        session.close()
    client = _client(agent_config_router.router, SessionLocal, current)
    assert client.get("/api/agent-config/").status_code == 403
    assert client.get("/api/agent-config/active").status_code == 403


def test_disabling_user_revokes_refresh_families():
    SessionLocal = _build_session()
    session = SessionLocal()
    try:
        admin = _user(session, "admin1", UserRole.ADMIN)
        student = _user(session, "student-disable", UserRole.STUDENT)
        auth_service.issue_token_pair(student)
        assert store.smembers(auth_service._user_families_key(student.id))
        student_id = student.id
    finally:
        session.close()

    client = _client(admin_router.router, SessionLocal, admin)
    response = client.put(
        f"/api/admin/users/{student_id}",
        json={"full_name": "停用学生", "role": "student", "grade": 1, "classroom_name": "1班", "is_active": False},
    )

    assert response.status_code == 200, response.text
    assert not store.smembers(auth_service._user_families_key(student_id))


def test_oidc_does_not_bind_by_email_local_part():
    SessionLocal = _build_session()
    session = SessionLocal()
    try:
        _user(session, "admin", UserRole.ADMIN)
        with pytest.raises(OidcAuthError):
            oidc_service.issue_local_tokens_for_claims(session, {"email": "admin@evil.test"})
    finally:
        session.close()


def test_rate_limit_buckets_are_per_account_when_token_is_valid(monkeypatch):
    from backend.security import create_access_token, create_refresh_token

    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(SlidingWindowRateLimitMiddleware, limit=1, window_seconds=60)
    seen: list[str] = []

    def fake_hit(key: str, limit: int, window_seconds: int) -> bool:
        seen.append(key)
        return seen.count(key) <= limit

    monkeypatch.setattr(store, "hit_sliding_window", fake_hit)
    client = TestClient(app)
    alice, _ = create_access_token("alice", "student")
    bob, _ = create_access_token("bob", "student")
    refresh, _ = create_refresh_token("carol", "student", "family")

    # 同一出口 IP 下不同账号各自计数
    assert client.get("/ping", headers={"Authorization": f"Bearer {alice}"}).status_code == 200
    assert client.get("/ping", headers={"Authorization": f"Bearer {bob}"}).status_code == 200
    assert client.get("/ping", headers={"Authorization": f"Bearer {alice}"}).status_code == 429
    # 伪造/非 access token 回落到 IP 计数，不能借用他人账号桶
    assert client.get("/ping", headers={"Authorization": "Bearer forged.token.value"}).status_code == 200
    assert client.get("/ping", headers={"Authorization": f"Bearer {refresh}"}).status_code == 429
    assert seen[0].startswith("user:alice:") and seen[1].startswith("user:bob:")
    assert seen[3].startswith("ip:") and seen[4] == seen[3]
