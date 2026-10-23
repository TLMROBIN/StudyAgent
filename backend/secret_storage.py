"""Provider credentials encrypted with a key stored separately from the database."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import TypeDecorator

from backend.config import get_settings

PREFIX = "encrypted:v1:"


def _cipher() -> Fernet:
    settings = get_settings()
    if settings.data_encryption_key:
        key = settings.data_encryption_key.encode("ascii")
    else:
        if settings.app_env.lower() in {"production", "prod"}:
            raise RuntimeError("DATA_ENCRYPTION_KEY is required in production")
        # Development only: atomically publish one random key shared by all workers.
        path = Path(settings.data_encryption_key_file)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
        path.chmod(0o600)
        key = path.read_bytes().strip()
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise RuntimeError("Invalid provider encryption key configuration") from None


def encrypt_secret(value: str) -> str:
    if not value:
        return value
    if value.startswith(PREFIX):
        # Do not accept damaged/wrong-key ciphertext as a usable API key.
        decrypt_secret(value)
        return value
    return PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        # Existing rows remain readable during the one-time upgrade.
        return value
    try:
        return _cipher().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise RuntimeError("Cannot decrypt provider credential; restore the matching DATA_ENCRYPTION_KEY") from None


class EncryptedSecret(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_secret(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return decrypt_secret(value) if value is not None else None


def encrypt_legacy_provider_keys(connection: Connection) -> int:
    """Idempotent, transaction-owned upgrade; never return or log key material."""
    count = 0
    for table in ("llm_provider_accounts", "llm_provider_configs"):
        if not inspect(connection).has_table(table):
            continue
        for row in connection.execute(text(f"SELECT id, api_key FROM {table}")).mappings().all():
            value = row["api_key"]
            if value and value.startswith(PREFIX):
                decrypt_secret(value)  # Fail closed before accepting an unreadable store.
            elif value:
                connection.execute(
                    text(f"UPDATE {table} SET api_key=:cipher WHERE id=:id"),
                    {"cipher": encrypt_secret(value), "id": row["id"]},
                )
                count += 1
    return count
