from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import learning_profile, user  # noqa: F401
from backend.models.learning_profile import StudentErrorEvent, StudentSkillProfile
from backend.models.user import User, UserRole
from backend.services.subject_profile_service import subject_profile_service


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _create_student(session: Session) -> User:
    student = User(username="student1", full_name="学生", role=UserRole.STUDENT, password_hash="hash")
    session.add(student)
    session.commit()
    session.refresh(student)
    return student


def test_record_math_error_event_updates_subject_profile():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        student = _create_student(session)

        event = subject_profile_service.record_error_event(
            session,
            student_id=student.id,
            subject="数学",
            evidence_text="应用题读不懂，不知道设什么为 x，也不会把数量关系转成方程。",
            conversation_id=7,
            message_id=11,
        )

        profile = session.scalar(select(StudentSkillProfile).where(StudentSkillProfile.student_id == student.id))
        assert event.subject == "数学"
        assert event.error_type == "word_problem_modeling"
        assert event.knowledge_point == "设元列方程"
        assert profile is not None
        assert profile.profile_json["top_error_label"] == "应用题建模困难"
        assert profile.profile_json["error_counts"]["word_problem_modeling"] == 1
    finally:
        session.close()


def test_english_vocabulary_dna_adds_unique_word_items():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        student = _create_student(session)

        result = subject_profile_service.add_english_vocabulary(
            session,
            student_id=student.id,
            source_text="把 photosynthesis 和 momentum 存入词汇DNA，photosynthesis 是光合作用。",
        )
        subject_profile_service.add_english_vocabulary(
            session,
            student_id=student.id,
            source_text="把 photosynthesis 再存一次",
        )

        profile = session.scalar(select(StudentSkillProfile).where(StudentSkillProfile.student_id == student.id))
        words = [item["word"] for item in profile.profile_json["vocabulary_items"]]
        assert result["added"] == ["photosynthesis", "momentum"]
        assert words.count("photosynthesis") == 1
        assert words.count("momentum") == 1
    finally:
        session.close()


def test_chinese_material_library_adds_tagged_material():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        student = _create_student(session)

        result = subject_profile_service.add_chinese_material(
            session,
            student_id=student.id,
            source_text="存入素材库：关于坚持的素材，苏轼被贬黄州后仍写出赤壁名篇。",
        )

        profile = session.scalar(select(StudentSkillProfile).where(StudentSkillProfile.student_id == student.id))
        item = profile.profile_json["material_items"][0]
        assert result["added_count"] == 1
        assert "坚持" in item["tags"]
        assert "苏轼" in item["text"]
    finally:
        session.close()


def test_record_error_event_rejects_unsupported_subjects():
    SessionLocal = _session_factory()
    session = SessionLocal()
    try:
        student = _create_student(session)

        try:
            subject_profile_service.record_error_event(
                session,
                student_id=student.id,
                subject="化学",
                evidence_text="方程式配平不会",
            )
        except ValueError as exc:
            assert "unsupported subject profile" in str(exc)
        else:
            raise AssertionError("Expected unsupported subjects to be rejected")

        assert session.scalars(select(StudentErrorEvent)).all() == []
    finally:
        session.close()
