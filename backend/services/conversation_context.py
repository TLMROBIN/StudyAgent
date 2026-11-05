"""Bounded, source-labelled conversation material outside the rolling chat window."""
from __future__ import annotations

import json

HISTORY_MESSAGE_LIMIT = 8


def task_context(history: list[tuple[str, str]]) -> str:
    users = [content.strip() for role, content in history if role == "user" and content.strip()]
    if not users:
        return ""
    # Preserve the source, not an assistant-generated account of what the student meant.
    first = users[0][:6000]
    updates = [text[:2000] for text in users[1:] if len(text) >= 24 or any(
        marker in text for marker in ("不是", "说错", "作文", "回去看", "换个题", "新问题")
    )][-3:]
    return json.dumps({"学生最初提供的材料与任务": first, "学生后续补充或纠正": updates}, ensure_ascii=False)


def stored_image_context(raw: str | None) -> str:
    try:
        data = json.loads(raw or "null")
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    context = data.get("_chat_context")
    if isinstance(context, dict) and isinstance(context.get("summary"), str):
        return json.dumps(context, ensure_ascii=False)[:6500]
    # Older structured vision records already contain useful source material.
    safe = {key: value for key, value in data.items() if key in {
        "question_text", "known_conditions", "options", "formulas_latex", "diagrams",
        "handwriting", "uncertainties", "quality_issues", "subject_guess",
    }}
    return json.dumps(safe, ensure_ascii=False)[:6500] if safe else ""


def image_context_record(result) -> str:
    data = dict(result.understanding_json or {})
    data["_chat_context"] = {
        "summary": result.prompt_summary[:6000],
        "confidence": result.confidence_level,
        "uncertainties": result.uncertainties,
    }
    return json.dumps(data, ensure_ascii=False)
