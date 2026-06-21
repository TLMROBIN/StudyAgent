from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models import feedback, user  # noqa: F401
from backend.models.feedback import ReleaseNote, StudentFeedbackReadState
from backend.models.user import Classroom, User, UserRole
from backend.routers import admin as admin_router
from backend.routers import feedback as feedback_router
from backend.routers import release_notes as release_notes_router


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def build_client(session_factory, current_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router.router)
    app.include_router(feedback_router.router)
    app.include_router(release_notes_router.router)

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


def create_user(session_factory, role: UserRole, *, full_name: str, classroom: Classroom | None = None) -> User:
    session = session_factory()
    try:
        item = User(
            username=f"{role.value}-{full_name}",
            full_name=full_name,
            role=role,
            password_hash="hash",
            grade=classroom.grade if classroom else None,
            classroom=classroom,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        session.expunge(item)
        return item
    finally:
        session.close()


def create_student_with_classroom(session_factory) -> User:
    session = session_factory()
    try:
        classroom = Classroom(grade=1, name="1班")
        session.add(classroom)
        session.commit()
        session.refresh(classroom)
        session.expunge(classroom)
    finally:
        session.close()
    return create_user(session_factory, UserRole.STUDENT, full_name="张三", classroom=classroom)


def test_student_feedback_limit_admin_reply_and_student_readback():
    session_factory = build_session()
    admin = create_user(session_factory, UserRole.ADMIN, full_name="管理员")
    student = create_student_with_classroom(session_factory)
    student_client = build_client(session_factory, student)
    admin_client = build_client(session_factory, admin)

    first = student_client.post("/api/feedback", json={"content": "希望错题能按章节筛选"})
    assert first.status_code == 201
    assert first.json()["content"] == "希望错题能按章节筛选"
    assert first.json()["reply_content"] is None

    second = student_client.post("/api/feedback", json={"content": "希望平板端按钮再大一点"})
    assert second.status_code == 201

    limited = student_client.post("/api/feedback", json={"content": "第三条今天不应该允许"})
    assert limited.status_code == 429
    assert "每天最多提交 2 次反馈" in limited.json()["detail"]

    admin_items = admin_client.get("/api/admin/feedback")
    assert admin_items.status_code == 200
    payload = admin_items.json()
    assert len(payload) == 2
    assert payload[0]["student_name"] == "张三"
    assert payload[0]["classroom_label"] == "高一1班"
    assert payload[0]["student_feedback_banned"] is False

    feedback_id = first.json()["id"]
    replied = admin_client.put(f"/api/admin/feedback/{feedback_id}/reply", json={"reply_content": "已收到，我们会排进优化清单。"})
    assert replied.status_code == 200
    assert replied.json()["reply_content"] == "已收到，我们会排进优化清单。"
    assert replied.json()["replied_by_name"] == "管理员"

    own_items = student_client.get("/api/feedback")
    assert own_items.status_code == 200
    assert own_items.json()["daily_remaining"] == 0
    first_item = next(item for item in own_items.json()["items"] if item["id"] == feedback_id)
    assert first_item["reply_content"] == "已收到，我们会排进优化清单。"


def test_admin_feedback_reply_sets_student_unread_until_marked_read():
    session_factory = build_session()
    admin = create_user(session_factory, UserRole.ADMIN, full_name="管理员")
    student = create_student_with_classroom(session_factory)
    student_client = build_client(session_factory, student)
    admin_client = build_client(session_factory, admin)

    created = student_client.post("/api/feedback", json={"content": "想知道更新了哪些功能"})
    assert created.status_code == 201
    feedback_id = created.json()["id"]
    assert student_client.get("/api/feedback/unread-summary").json()["unread_feedback_replies"] == 0

    replied = admin_client.put(f"/api/admin/feedback/{feedback_id}/reply", json={"reply_content": "请看更新日志入口。"})
    assert replied.status_code == 200
    assert replied.json()["student_unread"] is True

    summary = student_client.get("/api/feedback/unread-summary")
    assert summary.status_code == 200
    assert summary.json()["unread_feedback_replies"] == 1
    items = student_client.get("/api/feedback").json()["items"]
    assert next(item for item in items if item["id"] == feedback_id)["student_unread"] is True

    marked = student_client.post(f"/api/feedback/{feedback_id}/read")
    assert marked.status_code == 200
    assert marked.json()["student_unread"] is False
    assert student_client.get("/api/feedback/unread-summary").json()["unread_feedback_replies"] == 0

    session = session_factory()
    try:
        assert session.query(StudentFeedbackReadState).count() == 1
    finally:
        session.close()


def test_admin_can_ban_and_unban_student_feedback_permission():
    session_factory = build_session()
    admin = create_user(session_factory, UserRole.ADMIN, full_name="管理员")
    student = create_student_with_classroom(session_factory)
    student_client = build_client(session_factory, student)
    admin_client = build_client(session_factory, admin)

    banned = admin_client.post(f"/api/admin/feedback/students/{student.id}/ban", json={"reason": "重复提交无效内容"})
    assert banned.status_code == 200
    assert banned.json()["student_id"] == student.id
    assert banned.json()["is_banned"] is True

    blocked = student_client.post("/api/feedback", json={"content": "被封禁后不能提交"})
    assert blocked.status_code == 403
    assert "反馈权限已被管理员暂停" in blocked.json()["detail"]

    unbanned = admin_client.delete(f"/api/admin/feedback/students/{student.id}/ban")
    assert unbanned.status_code == 200
    assert unbanned.json()["is_banned"] is False

    allowed = student_client.post("/api/feedback", json={"content": "解除封禁后可以提交"})
    assert allowed.status_code == 201


def test_admin_can_publish_release_notes_and_student_read_state_tracks_unread():
    session_factory = build_session()
    admin = create_user(session_factory, UserRole.ADMIN, full_name="管理员")
    student = create_student_with_classroom(session_factory)
    student_client = build_client(session_factory, student)
    admin_client = build_client(session_factory, admin)

    draft = admin_client.post(
        "/api/admin/release-notes",
        json={
            "title": "物理答疑体验升级",
            "content": "新增图景优先引导、错因档案和对话内巩固练习。",
            "is_published": False,
        },
    )
    assert draft.status_code == 201
    assert student_client.get("/api/release-notes").json()["items"] == []

    published = admin_client.put(
        f"/api/admin/release-notes/{draft.json()['id']}",
        json={
            "title": "物理答疑体验升级",
            "content": "新增图景优先引导、错因档案和对话内巩固练习。",
            "is_published": True,
        },
    )
    assert published.status_code == 200
    assert published.json()["published_at"] is not None

    notes = student_client.get("/api/release-notes")
    assert notes.status_code == 200
    assert notes.json()["unread_count"] == 1
    assert notes.json()["items"][0]["is_read"] is False

    summary = student_client.get("/api/feedback/unread-summary")
    assert summary.status_code == 200
    assert summary.json()["unread_release_notes"] == 1
    assert summary.json()["has_unread"] is True

    marked = student_client.post(f"/api/release-notes/{published.json()['id']}/read")
    assert marked.status_code == 200
    assert marked.json()["is_read"] is True
    assert student_client.get("/api/feedback/unread-summary").json()["unread_release_notes"] == 0

    session = session_factory()
    try:
        note = session.query(ReleaseNote).one()
        assert note.created_by == admin.id
    finally:
        session.close()
