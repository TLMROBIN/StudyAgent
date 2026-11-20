"""Restricted first-question refusal samples without creating a student conversation."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re
import unicodedata

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.models.user import User
from backend.services.audit_service import audit_service
from scripts.topic_model.topic_model_common import mask_pii

logger = logging.getLogger(__name__)
_model_trace: ContextVar[dict | None] = ContextVar("first_refusal_model_trace", default=None)


@contextmanager
def trace_first_question(enabled: bool):
    trace = {} if enabled else None
    token = _model_trace.set(trace)
    try:
        yield trace
    finally:
        _model_trace.reset(token)


def record_model(**fields: str) -> None:
    trace = _model_trace.get()
    if trace is not None:
        trace.update(fields)


def classifier_provenance(model_key: str | None) -> dict[str, str]:
    fields = {"classifier_model_key": model_key or "default"}
    trace = _model_trace.get()
    if trace and trace.get("classifier_model"):
        fields["classifier_model"] = trace["classifier_model"]
    return fields


def _redacted_question(text: str, student: User) -> str:
    text = unicodedata.normalize("NFKC", text)
    for value in (student.full_name, student.student_no, student.username):
        if value:
            text = text.replace(value, "[已隐去]")
    text = re.sub(r"(?i)https?://[^\s，。；]+", "[链接]", text)
    text = re.sub(r"(?i)(密码|口令|验证码|token|api[_ -]?key)\s*[:=：是为]\s*[^\s，。；]+", r"\1：[已隐去]", text)
    text = re.sub(r"(?i)\b(?:bearer\s+[\w.~+/=-]+|sk-[\w-]{8,})", "[已隐去]", text)
    text = re.sub(r"(?:姓名|我叫|学号|考号)\s*[:：]?\s*[\w·-]+", "[已隐去]", text)
    return mask_pii(text)[:1200]


def log_first_refusal(db: Session, *, student: User, subject: str, question: str,
                      reason: str, has_image: bool, rule_id: str | None = None,
                      models: dict | None = None) -> None:
    try:
        audit_service.log(
            db, actor=None, action="first_question_refused", target_type="chat",
            target_id=None, result="blocked", ip_address=None,
            detail={"subject": subject, "question": _redacted_question(question, student),
                    "reason": reason, "has_image": has_image, "rule_id": rule_id,
                    "models": models or {}},
        )
    except SQLAlchemyError:
        db.rollback()
        # Database exception text can contain bound parameters and student content.
        logger.error("first_question_refusal_audit_write_failed")
