from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.user import User, UserRole
from backend.security import get_password_hash
from backend.services.oidc_service import OidcAuthError, oidc_service


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return SessionLocal


def test_oidc_callback_claims_issue_existing_student_token_pair():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        session.add(
            User(
                username="student001",
                student_no="S001",
                full_name="学生一",
                role=UserRole.STUDENT,
                password_hash=get_password_hash("legacy-password"),
            )
        )
        session.commit()

        tokens = oidc_service.issue_local_tokens_for_claims(
            session,
            {
                "iss": "http://10.50.159.62/auth/realms/school-platform",
                "sub": "keycloak-user-001",
                "preferred_username": "student001",
                "name": "学生一",
            },
        )

        assert tokens["access_token"]
        assert tokens["refresh_token"]
        assert tokens["must_change_password"] is False
    finally:
        session.close()


def test_oidc_callback_claims_reject_unbound_user():
    SessionLocal = build_session()
    session = SessionLocal()
    try:
        try:
            oidc_service.issue_local_tokens_for_claims(
                session,
                {
                    "iss": "http://10.50.159.62/auth/realms/school-platform",
                    "sub": "missing-user",
                    "preferred_username": "missing",
                },
            )
        except OidcAuthError as exc:
            assert "not bound" in str(exc)
        else:
            raise AssertionError("Expected OidcAuthError for unbound OIDC user")
    finally:
        session.close()
