from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.user import User, UserRole
from backend.services.student_grade_service import student_grade_service


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def test_ensure_user_grade_current_promotes_in_august():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        student = User(
            username="20260041",
            student_no="20260041",
            full_name="升级学生",
            role=UserRole.STUDENT,
            password_hash="hash",
            grade=1,
            last_grade_promotion_year=2025,
        )
        session.add(student)
        session.commit()
        session.refresh(student)

        changed = student_grade_service.ensure_user_grade_current(
            session,
            student,
            now=datetime.fromisoformat("2026-08-05T09:00:00+08:00"),
        )

        assert changed is True
        assert student.grade == 2
        assert student.grade_label == "高二"
        assert student.last_grade_promotion_year == 2026
    finally:
        session.close()


def test_ensure_user_grade_current_marks_grade_three_student_graduated():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        student = User(
            username="20260042",
            student_no="20260042",
            full_name="毕业学生",
            role=UserRole.STUDENT,
            password_hash="hash",
            grade=3,
            last_grade_promotion_year=2025,
        )
        session.add(student)
        session.commit()
        session.refresh(student)

        changed = student_grade_service.ensure_user_grade_current(
            session,
            student,
            now=datetime.fromisoformat("2026-08-05T09:00:00+08:00"),
        )

        assert changed is True
        assert student.grade is None
        assert student.is_graduated is True
        assert student.grade_label == "毕业"
        assert student.graduated_at is not None
        assert student.last_grade_promotion_year == 2026
    finally:
        session.close()


def test_apply_manual_grade_state_resets_graduation_and_promotion_cycle():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        student = User(
            username="20260043",
            student_no="20260043",
            full_name="回退学生",
            role=UserRole.STUDENT,
            password_hash="hash",
        )
        session.add(student)
        session.commit()
        session.refresh(student)

        student_grade_service.apply_manual_grade_state(
            student,
            grade=None,
            is_graduated=True,
            now=datetime.fromisoformat("2026-08-10T09:00:00+08:00"),
        )
        session.add(student)
        session.commit()
        session.refresh(student)

        assert student.is_graduated is True
        assert student.grade_label == "毕业"
        assert student.last_grade_promotion_year == 2026

        student_grade_service.apply_manual_grade_state(
            student,
            grade=2,
            is_graduated=False,
            now=datetime.fromisoformat("2026-09-10T09:00:00+08:00"),
        )

        assert student.grade == 2
        assert student.is_graduated is False
        assert student.graduated_at is None
        assert student.grade_label == "高二"
        assert student.last_grade_promotion_year == 2026
    finally:
        session.close()
