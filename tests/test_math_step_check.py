import asyncio
import json

import pytest

from backend.services.math_step_check import llm_service, math_step_evidence


@pytest.mark.parametrize(("expression", "source", "submission", "expected"), [
    ("3+1", "x+1", "f(3)等于2", "不一致"),
    ("3+1", "x+1", "f(3)=4", '"一致"'),
    ("0.3-0.1", "0.3-0.1", "0.2", '"一致"'),
    ("3/0", "x+1", "2", ""),
    ("x+1", "x+1", "2", ""),
    ("3+1", "无关依据", "4", ""),
])
def test_independent_step_check(monkeypatch, expression, source, submission, expected):
    async def extract(messages, **kwargs):
        assert submission not in messages[-1]["content"] or len(submission) == 1
        return json.dumps({"source": source, "expression": expression})

    monkeypatch.setattr(llm_service, "complete_response", extract)
    result = asyncio.run(math_step_evidence(submission, [("assistant", "这里要用 0.3-0.1，你算到多少？" if source == "0.3-0.1" else "这里要用 x+1，你算到多少？")], "", "test"))
    if expected:
        assert expected in result
    else:
        assert result == ""
