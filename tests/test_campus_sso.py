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
from backend.models.user import CampusIdentity, CampusRosterStudent, CampusRosterTeacher, User, UserRole
from backend.routers import campus_sso as campus_router
from backend.security import decode_token
from backend.services import campus_sso
from backend.services.store_service import MemoryStore
from scripts.import_campus_bindings import import_bindings, load_roster


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
    with session_factory() as db:
        db.add(CampusRosterStudent(school_id="school-1", number="S01", full_name="学生甲"))
        db.commit()
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
    assert 'localStorage.setItem("studyagent-campus-sso-session", "1")' in success.text
    assert 'localStorage.removeItem("studyagent-sso-session")' in success.text
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


def test_auto_create_requires_matching_roster_number_name_and_school(session_factory, tmp_path):
    roster_csv = tmp_path / "roster.csv"
    roster_csv.write_text("number,full_name\nS01,学生甲\n", encoding="utf-8")
    bindings = tmp_path / "bindings.csv"
    bindings.write_text("StudyAgent用户ID,StudyAgent账号,确认的中台学号\n", encoding="utf-8")
    roster = load_roster(roster_csv)
    with session_factory() as db:
        assert import_bindings(db, bindings, "school-1", apply=False, roster=roster) == (0, 0)
        assert db.scalar(select(CampusRosterStudent)) is None
        assert import_bindings(db, bindings, "school-1", apply=True, roster=roster) == (0, 0)
        for profile in (
            student_profile(number="not-listed"),
            campus_sso.CampusProfile("school-1", "student-id", "S01", "别的学生", UserRole.STUDENT),
            campus_sso.CampusProfile("school-2", "student-id", "S01", "学生甲", UserRole.STUDENT),
            campus_sso.CampusProfile("school-1", "teacher-id", "S01", "学生甲", UserRole.TEACHER),
        ):
            with pytest.raises(campus_sso.CampusSsoError):
                campus_sso.resolve_campus_user(db, profile, auto_create_students=True)
        assert db.scalar(select(User)) is None
        user = campus_sso.resolve_campus_user(db, student_profile(), auto_create_students=True)
        assert campus_sso.resolve_campus_user(db, student_profile(), auto_create_students=True).id == user.id
        assert db.scalars(select(User)).all() == [user]



def test_teacher_provisioning_reuses_directory_username_and_checks_role(session_factory):
    with session_factory() as db:
        db.add_all([
            CampusRosterTeacher(school_id="school-1", number="T01", username="teach-one", full_name="教师甲"),
            CampusRosterTeacher(school_id="school-1", number="T02", username="teach-two", full_name="教师乙"),
            CampusRosterTeacher(school_id="school-1", number="T03", username="existing-student", full_name="学生丙"),
        ])
        prior = User(username="teach-two", full_name="教师乙", role=UserRole.TEACHER, password_hash="old")
        student = User(username="existing-student", full_name="学生丙", role=UserRole.STUDENT, password_hash="old")
        db.add_all([prior, student])
        db.commit()
        first = campus_sso.CampusProfile("school-1", "teacher-1", "T01", "教师甲", UserRole.TEACHER)
        with pytest.raises(campus_sso.CampusSsoError):
            campus_sso.resolve_campus_user(db, first, auto_create_students=True)
        for number, name in (("T99", "教师甲"), ("T01", "其他姓名"), ("T03", "学生丙")):
            invalid = campus_sso.CampusProfile("school-1", "wrong", number, name, UserRole.TEACHER)
            with pytest.raises(campus_sso.CampusSsoError):
                campus_sso.resolve_campus_user(db, invalid, auto_create_students=True, auto_create_teachers=True)
        new = campus_sso.resolve_campus_user(db, first, auto_create_students=False, auto_create_teachers=True)
        assert new.username == "teach-one" and new.role == UserRole.TEACHER
        assert new.teacher_classrooms == [] and new.grade is None
        assert campus_sso.resolve_campus_user(db, first, auto_create_students=False, auto_create_teachers=True).id == new.id
        existing = campus_sso.CampusProfile("school-1", "teacher-2", "T02", "教师乙", UserRole.TEACHER)
        bound = campus_sso.resolve_campus_user(db, existing, auto_create_students=False, auto_create_teachers=True)
        assert bound.id == prior.id and bound.password_hash == "old"
        assert student.role == UserRole.STUDENT


def test_open_auto_provisioning_reuses_existing_students_and_rejects_conflicts(session_factory):
    def profile(external_id, number, name):
        return campus_sso.CampusProfile("school-1", external_id, number, name, UserRole.STUDENT)

    with session_factory() as db:
        old = User(username="S14", student_no="S14", full_name="已有学生", role=UserRole.STUDENT, password_hash="old", grade=3)
        db.add(old)
        db.commit()
        options = dict(auto_create_students=True, allow_unlisted_students=True, blocked_student_numbers=frozenset({"removed"}))
        for conflicting in (
            profile("case-1", "S14", "其他姓名"),
            profile("case-2", "new-number", "已有学生"),
            profile("case-3", "removed", "已排除学生"),
        ):
            with pytest.raises(campus_sso.CampusSsoError):
                campus_sso.resolve_campus_user(db, conflicting, **options)
        assert db.scalars(select(User)).all() == [old]

        resumed = campus_sso.resolve_campus_user(db, profile("existing", "S14", "已有学生"), **options)
        assert resumed.id == old.id and resumed.grade == 3 and resumed.password_hash == "old"
        first_login = profile("new", "not-listed", "首次登录学生")
        new = campus_sso.resolve_campus_user(db, first_login, **options)
        assert campus_sso.resolve_campus_user(db, first_login, **options).id == new.id
        assert db.scalar(select(CampusIdentity).where(CampusIdentity.user_id == new.id)).number == "not-listed"
        with pytest.raises(campus_sso.CampusSsoError):
            campus_sso.resolve_campus_user(db, profile("other-subject", "not-listed", "首次登录学生"), **options)
