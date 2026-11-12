from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.user import User, UserRole
from backend.security import decode_token, get_password_hash, verify_password
from backend.services.auth_service import auth_service
from backend.services.store_service import MemoryStore


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def test_password_hash_roundtrip_for_normal_password():
    secret = "StudyAgent123"
    hashed = get_password_hash(secret)
    assert hashed.startswith("$2")
    assert verify_password(secret, hashed) is True


def test_password_hash_roundtrip_for_long_password():
    secret = "x" * 100
    hashed = get_password_hash(secret)
    assert hashed.startswith("bcrypt_sha256$")
    assert verify_password(secret, hashed) is True


def test_authenticate_returns_none_for_naive_locked_until_from_sqlite():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        student = User(
            username="locked-student",
            full_name="Locked Student",
            role=UserRole.STUDENT,
            password_hash=get_password_hash("correct-password"),
            failed_login_count=5,
            locked_until=datetime.now() + timedelta(minutes=15),
        )
        session.add(student)
        session.commit()

        authenticated = auth_service.authenticate_student(session, "locked-student", "correct-password", "127.0.0.1")

        assert authenticated is None
    finally:
        session.close()


def test_authenticate_clears_expired_naive_locked_until_from_sqlite():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        student = User(
            username="expired-lock-student",
            full_name="Expired Lock Student",
            role=UserRole.STUDENT,
            password_hash=get_password_hash("correct-password"),
            failed_login_count=5,
            locked_until=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        )
        session.add(student)
        session.commit()

        authenticated = auth_service.authenticate_student(session, "expired-lock-student", "correct-password", "127.0.0.1")

        assert authenticated is not None
        assert authenticated.failed_login_count == 0
        assert authenticated.locked_until is None
    finally:
        session.close()


def test_second_login_supersedes_first_device_without_cross_logout(monkeypatch):
    memory_store = MemoryStore()
    monkeypatch.setattr("backend.services.auth_service.store", memory_store)
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        student = User(
            username="single-device-student",
            full_name="Single Device Student",
            role=UserRole.STUDENT,
            password_hash="sso-managed",
        )
        session.add(student)
        session.commit()

        first = auth_service.issue_token_pair(student)
        first_payload = decode_token(first["access_token"])
        assert auth_service.is_current_family(student.id, first_payload["family_id"])

        second = auth_service.issue_token_pair(student)
        second_payload = decode_token(second["access_token"])
        assert not auth_service.is_current_family(student.id, first_payload["family_id"])
        assert auth_service.is_current_family(student.id, second_payload["family_id"])

        old_user, old_tokens = auth_service.rotate_refresh_token(session, decode_token(first["refresh_token"]))
        assert old_user is None and old_tokens is None
        assert auth_service.is_current_family(student.id, second_payload["family_id"])

        new_user, new_tokens = auth_service.rotate_refresh_token(session, decode_token(second["refresh_token"]))
        assert new_user == student and new_tokens is not None
        assert decode_token(new_tokens["access_token"])["family_id"] == second_payload["family_id"]
    finally:
        session.close()
