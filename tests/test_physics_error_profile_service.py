from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import learning_profile, user  # noqa: F401
from backend.models.learning_profile import StudentErrorEvent, StudentSkillProfile
from backend.models.user import User, UserRole
from backend.services.physics_error_profile_service import physics_error_profile_service


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_record_event_classifies_and_updates_student_physics_profile():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        student = User(username="student1", full_name="学生", role=UserRole.STUDENT, password_hash="hash")
        session.add(student)
        session.commit()
        session.refresh(student)

        event = physics_error_profile_service.record_event(
            session,
            student_id=student.id,
            subject="物理",
            evidence_text="这道题我没画受力图，漏掉了摩擦力。",
            conversation_id=7,
            message_id=11,
        )

        profile = session.scalar(select(StudentSkillProfile).where(StudentSkillProfile.student_id == student.id))
        assert event.error_type == "diagram_establishment"
        assert event.knowledge_point == "受力分析"
        assert profile is not None
        assert profile.profile_json["top_error_type"] == "diagram_establishment"
        assert profile.profile_json["error_counts"]["diagram_establishment"] == 1
    finally:
        session.close()


def test_profile_summary_returns_empty_shape_without_events():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        summary = physics_error_profile_service.profile_summary(session, student_id=999, subject="物理")

        assert summary == {
            "subject": "物理",
            "total_events": 0,
            "top_error_type": None,
            "top_error_label": None,
            "error_counts": {},
            "recent_weaknesses": [],
        }
    finally:
        session.close()


def test_record_event_is_physics_only():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        student = User(username="student1", full_name="学生", role=UserRole.STUDENT, password_hash="hash")
        session.add(student)
        session.commit()
        session.refresh(student)

        try:
            physics_error_profile_service.record_event(
                session,
                student_id=student.id,
                subject="数学",
                evidence_text="公式用错了",
            )
        except ValueError as exc:
            assert "only supports physics" in str(exc)
        else:
            raise AssertionError("Expected non-physics records to be rejected")

        assert session.scalars(select(StudentErrorEvent)).all() == []
    finally:
        session.close()
