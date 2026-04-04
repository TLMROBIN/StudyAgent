from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.models.user import User, UserRole
from backend.security import create_access_token, create_refresh_token, get_password_hash, verify_password
from backend.services.store_service import store


class AuthService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def seed_bootstrap_admin(self, db: Session) -> User:
        admin = db.scalar(select(User).where(User.role == UserRole.ADMIN).limit(1))
        if admin:
            return admin

        admin = User(
            username=self.settings.bootstrap_admin_username,
            full_name="系统管理员",
            role=UserRole.ADMIN,
            password_hash=get_password_hash(self.settings.bootstrap_admin_password),
            must_change_password=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        return admin

    def get_user_by_subject(self, db: Session, subject: str) -> User | None:
        return db.scalar(select(User).where(User.username == subject).limit(1))

    def get_user_by_student_no(self, db: Session, student_no: str) -> User | None:
        return db.scalar(select(User).where(User.student_no == student_no).limit(1))

    def authenticate_student(self, db: Session, student_no: str, password: str, client_ip: str | None = None) -> User | None:
        user = self.get_user_by_student_no(db, student_no)
        return self._authenticate(db, user, password, client_ip)

    def authenticate_staff(self, db: Session, username: str, password: str, client_ip: str | None = None) -> User | None:
        user = db.scalar(select(User).where(User.username == username).limit(1))
        if user and user.role == UserRole.STUDENT:
            return None
        return self._authenticate(db, user, password, client_ip)

    def _authenticate(self, db: Session, user: User | None, password: str, client_ip: str | None = None) -> User | None:
        if not user or not user.is_active:
            return None
        if user.locked_until and user.locked_until > datetime.now(UTC):
            return None
        attempt_key = f"login:{user.id}:{client_ip or 'unknown'}"
        attempts = int(store.get(attempt_key) or "0")
        if attempts >= 10:
            return None

        if not verify_password(password, user.password_hash):
            user.failed_login_count += 1
            store.set(attempt_key, str(attempts + 1), ttl_seconds=60)
            if user.failed_login_count >= 5:
                user.locked_until = datetime.now(UTC) + timedelta(minutes=15)
            db.add(user)
            db.commit()
            return None

        user.failed_login_count = 0
        user.locked_until = None
        db.add(user)
        db.commit()
        return user

    def issue_token_pair(self, user: User) -> dict[str, str | int | bool]:
        family_id = str(uuid4())
        access_token, access_jti = create_access_token(user.username, user.role.value)
        refresh_token, refresh_jti = create_refresh_token(user.username, user.role.value, family_id)

        refresh_ttl = self.settings.refresh_token_expire_days * 24 * 60 * 60
        store.set(self._refresh_key(user.id, family_id), refresh_jti, ttl_seconds=refresh_ttl)
        store.sadd(self._user_families_key(user.id), family_id)
        store.set(self._access_subject_key(access_jti), user.username, ttl_seconds=self.settings.access_token_expire_minutes * 60)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in_seconds": self.settings.access_token_expire_minutes * 60,
            "must_change_password": user.must_change_password,
        }

    def rotate_refresh_token(self, db: Session, refresh_payload: dict) -> tuple[User | None, dict[str, str | int | bool] | None]:
        if refresh_payload.get("type") != "refresh":
            return None, None

        user = self.get_user_by_subject(db, refresh_payload.get("sub", ""))
        if not user:
            return None, None

        family_id = refresh_payload.get("family_id", "")
        presented_jti = refresh_payload.get("jti", "")
        current_jti = store.get(self._refresh_key(user.id, family_id))
        if not current_jti:
            return None, None
        if current_jti != presented_jti:
            self.revoke_all_families(user.id)
            return None, None

        next_access_token, next_access_jti = create_access_token(user.username, user.role.value)
        next_refresh_token, next_refresh_jti = create_refresh_token(user.username, user.role.value, family_id)
        refresh_ttl = self.settings.refresh_token_expire_days * 24 * 60 * 60
        store.set(self._refresh_key(user.id, family_id), next_refresh_jti, ttl_seconds=refresh_ttl)
        store.set(self._access_subject_key(next_access_jti), user.username, ttl_seconds=self.settings.access_token_expire_minutes * 60)
        return user, {
            "access_token": next_access_token,
            "refresh_token": next_refresh_token,
            "expires_in_seconds": self.settings.access_token_expire_minutes * 60,
            "must_change_password": user.must_change_password,
        }

    def revoke_family(self, user_id: int, family_id: str) -> None:
        store.delete(self._refresh_key(user_id, family_id))
        store.srem(self._user_families_key(user_id), family_id)

    def revoke_all_families(self, user_id: int) -> None:
        for family_id in store.smembers(self._user_families_key(user_id)):
            store.delete(self._refresh_key(user_id, family_id))
        store.delete_set(self._user_families_key(user_id))

    def revoke_access_token(self, access_jti: str, ttl_seconds: int) -> None:
        store.set(self._blacklist_key(access_jti), "1", ttl_seconds=ttl_seconds)

    def is_access_token_revoked(self, access_jti: str) -> bool:
        return store.get(self._blacklist_key(access_jti)) == "1"

    def password_changed_after_token(self, user: User, payload: dict) -> bool:
        issued_at = payload.get("iat")
        if not issued_at:
            return False
        return int(user.password_changed_at.timestamp()) > int(issued_at)

    def update_password(self, db: Session, user: User, new_password: str) -> User:
        user.password_hash = get_password_hash(new_password)
        user.must_change_password = False
        user.password_changed_at = datetime.now(UTC)
        db.add(user)
        db.commit()
        db.refresh(user)
        self.revoke_all_families(user.id)
        return user

    @staticmethod
    def _refresh_key(user_id: int, family_id: str) -> str:
        return f"refresh:{user_id}:{family_id}"

    @staticmethod
    def _user_families_key(user_id: int) -> str:
        return f"user_families:{user_id}"

    @staticmethod
    def _blacklist_key(access_jti: str) -> str:
        return f"blacklist:{access_jti}"

    @staticmethod
    def _access_subject_key(access_jti: str) -> str:
        return f"access_subject:{access_jti}"


auth_service = AuthService()
