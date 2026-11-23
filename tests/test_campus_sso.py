from io import BytesIO
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings
from backend.database import Base, get_db
from backend.models.user import CampusIdentity, User, UserRole
from backend.routers import campus_sso as campus_router
from backend.security import decode_token
from backend.services import campus_sso
from backend.services.store_service import MemoryStore
from scripts.import_campus_bindings import import_bindings


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, class_=Session, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        campus_sso_token_url="https://center.example.test/api",
        campus_sso_school_id="school-1",
        campus_sso_project_id="studyagent-1",
    )


def student_profile(external_id="student-id", number="S01"):
    return campus_sso.CampusProfile("school-1", external_id, number, "学生甲", UserRole.STUDENT)


def test_verifies_provider_scope_and_does_not_follow_redirects(monkeypatch, settings):
    payload = {
        "result": "0",
        "data": {
            "scid": "school-1",
            "projectID": "studyagent-1",
            "_id": "student-id",
            "number": "S01",
            "name": "学生甲",
            "userType": 3,
        },
    }

    class FakeOpener:
        def open(self, request, *, timeout):
            assert timeout == 5
            assert parse_qs(urlsplit(request.full_url).query) == {
                "cmd": ["API"], "mode": ["getTokenInfo"], "cfg": ["opaque+/="]
            }
            return BytesIO(json.dumps(payload).encode())

    def fake_builder(handler):
        assert handler.redirect_request(None, None, None, None, None, None) is None
        return FakeOpener()

    monkeypatch.setattr(campus_sso, "build_opener", fake_builder)
    assert campus_sso.verify_campus_token("opaque+/=", settings) == student_profile()
    payload["data"]["projectID"] = "other-project"
    with pytest.raises(campus_sso.CampusSsoError):
        campus_sso.verify_campus_token("opaque+/=", settings)
    payload["data"]["projectID"] = "studyagent-1"
    payload["data"]["userType"] = 1
    with pytest.raises(campus_sso.CampusSsoError):
        campus_sso.verify_campus_token("opaque+/=", settings)


def test_imported_old_user_keeps_same_id_and_binding(session_factory, tmp_path):
    with session_factory() as db:
        old = User(username="old", full_name="学生甲", role=UserRole.STUDENT, password_hash="old")
        db.add(old)
        db.commit()
        csv_path = tmp_path / "map.csv"
        csv_path.write_text(
            f"StudyAgent用户ID,StudyAgent账号,确认的中台学号\n{old.id},old,S01\n",
            encoding="utf-8-sig",
        )
        assert import_bindings(db, csv_path, "school-1", apply=False) == (1, 0)
        assert db.scalar(select(CampusIdentity)) is None
        assert import_bindings(db, csv_path, "school-1", apply=True) == (1, 0)
        assert import_bindings(db, csv_path, "school-1", apply=True) == (0, 1)
        user = campus_sso.resolve_campus_user(db, student_profile(), auto_create_students=False)
        assert user.id == old.id
        assert user.username == "old"
        user = campus_sso.resolve_campus_user(db, student_profile(number="S01-changed"), auto_create_students=False)
        assert user.id == old.id
        with pytest.raises(campus_sso.CampusSsoError):
            campus_sso.resolve_campus_user(db, student_profile(external_id="impostor"), auto_create_students=True)
        user.is_active = False
        db.commit()
        with pytest.raises(campus_sso.CampusSsoError):
            campus_sso.resolve_campus_user(db, student_profile(), auto_create_students=True)


def test_http_handoff_only_enrolls_after_browser_confirmation(session_factory, monkeypatch, settings):
    settings.campus_sso_auto_create_students = True
    settings.web_base_path = "/studyagent/"
    monkeypatch.setattr(campus_router, "get_settings", lambda: settings)
    monkeypatch.setattr(campus_router, "store", MemoryStore())
    monkeypatch.setattr(campus_router, "verify_campus_token", lambda token, cfg: student_profile())
    app = FastAPI()
    app.include_router(campus_router.router, prefix="/studyagent")

    def db_override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    client = TestClient(app, base_url="https://studyagent.example.test")
    callback = client.get("/studyagent/sso?token=external-test", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == "sso/confirm"
    assert "external-test" not in callback.text
    assert callback.headers["referrer-policy"] == "no-referrer"
    assert "Secure" in callback.headers["set-cookie"]
    form = client.get(urljoin(str(callback.url), callback.headers["location"]))
    nonce = re.search(r'name="confirmation" value="([^"]+)"', form.text).group(1)
    assert form.headers["x-frame-options"] == "DENY"
    assert client.post(form.url.path, data={"confirmation": "wrong"}).status_code == 403
    with session_factory() as db:
        assert db.scalar(select(User)) is None
    success = client.post(form.url.path, data={"confirmation": nonce})
    assert success.status_code == 200
    token = json.loads(re.search(r'setItem\("studyagent-access-token", ([^;]+)\);', success.text).group(1))
    with session_factory() as db:
        user = db.scalar(select(User))
        uid = user.id
        assert decode_token(token)["sub"] == user.username
        assert campus_sso.resolve_campus_user(db, student_profile(), auto_create_students=True).id == uid
        assert len(db.scalars(select(User)).all()) == 1
    assert client.post(form.url.path, data={"confirmation": nonce}).status_code == 403
    insecure = TestClient(app, base_url="http://studyagent.example.test")
    assert insecure.get("/studyagent/sso?token=external-test").status_code == 503
    settings.campus_sso_allow_http = True
    callback = insecure.get("/studyagent/sso?token=external-test", follow_redirects=False)
    assert callback.status_code == 303
    assert "Secure" not in callback.headers["set-cookie"]
    form = insecure.get(urljoin(str(callback.url), callback.headers["location"]))
    nonce = re.search(r'name="confirmation" value="([^"]+)"', form.text).group(1)
    assert insecure.post(form.url.path, data={"confirmation": nonce}).status_code == 200
