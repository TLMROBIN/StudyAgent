import warnings

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic.warnings import UnsupportedFieldAttributeWarning

from backend.models.schemas import StudentLoginRequest


def test_student_login_request_accepts_current_and_legacy_fields_without_mutating_input():
    current = StudentLoginRequest.model_validate({"username": "current", "password": "secret"})
    assert current.username == "current"

    legacy_payload = {"student_no": "legacy", "password": "secret"}
    assert StudentLoginRequest.model_validate(legacy_payload).username == "legacy"
    assert legacy_payload == {"student_no": "legacy", "password": "secret"}


def test_student_login_request_keeps_explicit_username_priority():
    payload = StudentLoginRequest.model_validate(
        {"username": "current", "student_no": "legacy", "password": "secret"}
    )
    empty_username = StudentLoginRequest.model_validate(
        {"username": "", "student_no": "legacy", "password": "secret"}
    )

    assert payload.username == "current"
    assert empty_username.username == ""


def test_student_login_request_does_not_emit_unsupported_field_warning_in_fastapi():
    app = FastAPI()

    @app.post("/login")
    def login(payload: StudentLoginRequest) -> dict[str, str]:
        return payload.model_dump()

    with warnings.catch_warnings():
        warnings.simplefilter("error", UnsupportedFieldAttributeWarning)
        response = TestClient(app).post("/login", json={"student_no": "legacy", "password": "secret"})

    assert response.status_code == 200
    assert response.json() == {"username": "legacy", "password": "secret"}
