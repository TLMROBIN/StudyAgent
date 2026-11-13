from __future__ import annotations

import json

from sqlalchemy.orm import Session

from backend.models.system_config import SystemConfig
from backend.subjects import SUBJECTS

ENABLED_SUBJECTS_KEY = "chat.enabled_subjects"
SUBJECT_DISABLED_MESSAGE = "该学科已停用，请选择其他已启用学科。"


def get_enabled_subjects(db: Session) -> list[str]:
    config = db.get(SystemConfig, ENABLED_SUBJECTS_KEY)
    if config is None:
        return list(SUBJECTS)
    try:
        enabled = json.loads(config.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(enabled, list):
        return []
    return [subject for subject in SUBJECTS if subject in enabled]
