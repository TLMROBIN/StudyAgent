from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models import notification, user  # noqa: F401
from backend.models.user import User, UserRole
from backend.routers import admin as admin_router
from backend.routers import notifications as notifications_router


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def build_client(session_factory, current_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router.router)
    app.include_router(notifications_router.router)

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


def create_user(session_factory, role: UserRole) -> User:
    session = session_factory()
    try:
        user = User(
            username=f"{role.value}-user",
            full_name=f"{role.value} user",
            role=role,
            password_hash="hash",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user
    finally:
        session.close()


def test_admin_manages_schoolwide_notifications_and_students_only_see_active_items():
    session_factory = build_session()
    admin = create_user(session_factory, UserRole.ADMIN)
    student = create_user(session_factory, UserRole.STUDENT)
    admin_client = build_client(session_factory, admin)
    student_client = build_client(session_factory, student)

    created = admin_client.post(
        "/api/admin/notifications",
        json={"title": "晚自习安排", "content": "今晚 19:00-20:30 开放物理答疑。"},
    )
    assert created.status_code == 201
    created_payload = created.json()
    assert created_payload["title"] == "晚自习安排"
    assert created_payload["content"] == "今晚 19:00-20:30 开放物理答疑。"
    assert created_payload["is_archived"] is False

    student_active = student_client.get("/api/notifications/active")
    assert student_active.status_code == 200
    assert [item["title"] for item in student_active.json()] == ["晚自习安排"]

    notification_id = created_payload["id"]
    updated = admin_client.put(
        f"/api/admin/notifications/{notification_id}",
        json={"title": "晚自习安排更新", "content": "今晚 19:15 开始，地点改为物理实验室。"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "晚自习安排更新"

    archived = admin_client.post(f"/api/admin/notifications/{notification_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["is_archived"] is True

    assert student_client.get("/api/notifications/active").json() == []
    admin_items = admin_client.get("/api/admin/notifications").json()
    assert len(admin_items) == 1
    assert admin_items[0]["is_archived"] is True
