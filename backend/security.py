import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import bcrypt
from jose import JWTError, jwt

from backend.config import get_settings

LONG_PASSWORD_PREFIX = "bcrypt_sha256$"


def _normalize_password(password: str) -> tuple[bytes, str]:
    password_bytes = password.encode("utf-8")
    if len(password_bytes) <= 72:
        return password_bytes, ""
    digest = hashlib.sha256(password_bytes).hexdigest().encode("utf-8")
    return digest, LONG_PASSWORD_PREFIX


def verify_password(plain_password: str, hashed_password: str) -> bool:
    secret = plain_password
    hash_value = hashed_password
    if hashed_password.startswith(LONG_PASSWORD_PREFIX):
        secret = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
        hash_value = hashed_password[len(LONG_PASSWORD_PREFIX) :]
    try:
        return bcrypt.checkpw(secret.encode("utf-8"), hash_value.encode("utf-8"))
    except ValueError:
        return False


def get_password_hash(password: str) -> str:
    normalized, prefix = _normalize_password(password)
    hashed = bcrypt.hashpw(normalized, bcrypt.gensalt(rounds=12)).decode("utf-8")
    return f"{prefix}{hashed}"


def create_access_token(subject: str, role: str) -> tuple[str, str]:
    settings = get_settings()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_expire_minutes)
    jti = str(uuid4())
    payload = {
        "sub": subject,
        "role": role,
        "type": "access",
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti


def create_refresh_token(subject: str, role: str, family_id: str) -> tuple[str, str]:
    settings = get_settings()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(days=settings.refresh_token_expire_days)
    jti = str(uuid4())
    payload = {
        "sub": subject,
        "role": role,
        "type": "refresh",
        "family_id": family_id,
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("Invalid token") from exc
