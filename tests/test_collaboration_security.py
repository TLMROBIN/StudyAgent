"""Security boundaries use synthetic data and independent temporary encryption keys."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings
from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models.llm_account import LLMProviderAccount
from backend.models.llm_provider import LLMProviderConfig
from backend.models.user import Classroom, User, UserRole
from backend.routers import stats
from backend import secret_storage


@pytest.fixture
def encryption(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        app_env="development", data_encryption_key=Fernet.generate_key().decode(),
        data_encryption_key_file=str(tmp_path / "secrets" / "provider.key"),
    )
    monkeypatch.setattr(secret_storage, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine, sessionmaker(engine, class_=Session, expire_on_commit=False)
    engine.dispose()


@pytest.mark.parametrize("model,table", [(LLMProviderAccount, "llm_provider_accounts"), (LLMProviderConfig, "llm_provider_configs")])
def test_provider_key_is_encrypted_on_create_and_update(encryption, database, model, table):
    engine, factory = database
    values = {"base_url": "https://example.invalid/v1", "api_key": "synthetic-first-secret"}
    if model is LLMProviderAccount:
        values.update(provider_name="synthetic", display_name="Synthetic")
    else:
        values.update(name="synthetic", model="synthetic")
    with factory() as db:
        row = model(**values)
        db.add(row)
        db.commit()
        row_id = row.id
    with engine.connect() as connection:
        raw = connection.scalar(text(f"SELECT api_key FROM {table} WHERE id=:id"), {"id": row_id})
        assert raw.startswith(secret_storage.PREFIX)
        assert "synthetic-first-secret" not in raw
    with factory() as db:
        row = db.get(model, row_id)
        assert row.api_key == "synthetic-first-secret"
        row.api_key = "synthetic-replacement-secret"
        db.commit()
    with factory() as db:
        assert db.get(model, row_id).api_key == "synthetic-replacement-secret"


def test_legacy_key_migration_is_atomic_idempotent_and_preserves_values(encryption, database):
    engine, _ = database
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO llm_provider_configs (id,name,base_url,api_key,model,is_active,is_fallback) VALUES (1,'test','https://example.invalid','legacy-synthetic-secret','test',1,0)"))
        assert secret_storage.encrypt_legacy_provider_keys(connection) == 1
        assert secret_storage.encrypt_legacy_provider_keys(connection) == 0
        cipher = connection.scalar(text("SELECT api_key FROM llm_provider_configs WHERE id=1"))
        assert secret_storage.decrypt_secret(cipher) == "legacy-synthetic-secret"
    original_key = encryption.data_encryption_key
    encryption.data_encryption_key = Fernet.generate_key().decode()
    with pytest.raises(RuntimeError, match="Cannot decrypt"):
        secret_storage.decrypt_secret(cipher)
    encryption.data_encryption_key = original_key
    assert secret_storage.decrypt_secret(cipher) == "legacy-synthetic-secret"


def test_development_key_is_persisted_privately_and_reused(encryption):
    encryption.data_encryption_key = None
    cipher = secret_storage.encrypt_secret("synthetic-development-key")
    path = Path(encryption.data_encryption_key_file)
    assert path.stat().st_mode & 0o777 == 0o600
    saved_key = path.read_bytes()
    assert secret_storage.decrypt_secret(cipher) == "synthetic-development-key"
    assert path.read_bytes() == saved_key


@pytest.mark.parametrize("overrides", [
    {"JWT_SECRET_KEY": "change-me"},
    {"DATA_ENCRYPTION_KEY": None},
    {"DATA_ENCRYPTION_KEY": "invalid-secret-value"},
    {"BOOTSTRAP_ADMIN_PASSWORD": "StudyAgent123"},
])
def test_production_refuses_insecure_configuration_without_echoing_secrets(overrides):
    values = {"APP_ENV": "production", "JWT_SECRET_KEY": "synthetic-jwt-secret-with-at-least-32-characters",
              "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(), "BOOTSTRAP_ADMIN_PASSWORD": "synthetic-unique-admin"}
    values.update(overrides)
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, **values)
    assert "invalid-secret-value" not in str(error.value)
    assert "synthetic-unique-admin" not in str(error.value)


def test_profile_access_enforces_teacher_class_student_self_and_admin_boundaries(database):
    _, factory = database
    with factory() as db:
        assigned = Classroom(grade=1, name="audit-A")
        other = Classroom(grade=1, name="audit-B")
        teacher = User(username="audit-teacher", full_name="Synthetic Teacher", role=UserRole.TEACHER, password_hash="unused")
        teacher.teacher_classrooms.append(assigned)
        unassigned_teacher = User(username="no-class", full_name="No Class", role=UserRole.TEACHER, password_hash="unused")
        admin = User(username="audit-admin", full_name="Admin", role=UserRole.ADMIN, password_hash="unused")
        a = User(username="audit-A", full_name="A", role=UserRole.STUDENT, password_hash="unused", classroom=assigned)
        b = User(username="audit-B", full_name="B", role=UserRole.STUDENT, password_hash="unused", classroom=other)
        db.add_all([assigned, other, teacher, unassigned_teacher, admin, a, b])
        db.commit()
        app = FastAPI()
        app.include_router(stats.router)
        viewer = [teacher]
        def session():
            with factory() as current:
                yield current
        app.dependency_overrides[get_db] = session
        app.dependency_overrides[get_current_user] = lambda: viewer[0]
        with TestClient(app) as client:
            assert client.get(f"/api/stats/students/{a.id}").status_code == 200
            assert client.get(f"/api/stats/students/{b.id}").status_code == 403
            assert client.get("/api/stats/students/999999").status_code == 403
            viewer[0] = unassigned_teacher
            assert client.get(f"/api/stats/students/{a.id}").status_code == 403
            viewer[0] = a
            assert client.get(f"/api/stats/students/{a.id}").status_code == 200
            assert client.get(f"/api/stats/students/{b.id}").status_code == 403
            viewer[0] = admin
            assert client.get(f"/api/stats/students/{b.id}").status_code == 200


@pytest.mark.parametrize("name,content", [
    (".env.bak-sso", "JWT_SECRET_KEY=synthetic-local-only"),
    ("artifacts/live.json", '{"student":"synthetic"}'),
    ("notes.md", "sk-" + "A" * 24),
])
def test_commit_guard_rejects_sensitive_index_content_without_printing_values(tmp_path, name, content):
    guard = Path("scripts/check_no_credentials.py").resolve()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "add", name], cwd=tmp_path, check=True)
    target.write_text("working tree no longer contains the staged secret")
    result = subprocess.run([sys.executable, str(guard), "--staged"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert name in result.stderr
    assert content not in result.stdout + result.stderr
