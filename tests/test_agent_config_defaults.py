import json
from pathlib import Path

from backend.routers.agent_config import (
    SUBJECT_PROMPT_DEFAULTS_PATH,
    get_subject_prompt_defaults,
)

ALL_SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"]


def test_subject_prompt_defaults_file_is_valid_json_with_all_subjects():
    raw = json.loads(Path(SUBJECT_PROMPT_DEFAULTS_PATH).read_text(encoding="utf-8"))

    assert isinstance(raw, dict)
    for subject in ALL_SUBJECTS:
        assert subject in raw, f"缺少 {subject} 模板"
        assert isinstance(raw[subject], str)
        assert raw[subject].strip(), f"{subject} 模板为空"


def test_subject_prompt_defaults_endpoint_returns_nine_subjects():
    payload = get_subject_prompt_defaults(current_user=None)

    assert set(ALL_SUBJECTS) <= set(payload.keys())
    assert all(isinstance(v, str) and v.strip() for v in payload.values())


def test_subject_prompt_defaults_never_promise_final_answers():
    payload = get_subject_prompt_defaults(current_user=None)

    for subject, text in payload.items():
        assert "直接给出最终答案" not in text
        assert "留给学生" in text or "由学生自己" in text or "自己完成" in text or "自己" in text, subject


def test_subject_prompt_defaults_endpoint_survives_missing_file(monkeypatch, tmp_path):
    import backend.routers.agent_config as module

    monkeypatch.setattr(module, "SUBJECT_PROMPT_DEFAULTS_PATH", tmp_path / "missing.json")
    assert get_subject_prompt_defaults(current_user=None) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(module, "SUBJECT_PROMPT_DEFAULTS_PATH", broken)
    assert get_subject_prompt_defaults(current_user=None) == {}
