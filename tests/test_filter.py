import json
from pathlib import Path

from backend.services.filter_service import filter_service


def test_adversarial_cases_match_expectation():
    cases = json.loads(Path("tests/adversarial_cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 50
    for case in cases:
        decision = filter_service.check_question(case["text"])
        assert decision.allowed is case["allowed"], case["text"]


def test_output_validator_blocks_direct_answer():
    validation = filter_service.validate_answer("最终答案是 A，所以你直接选这个。")
    assert not validation.allowed
    assert "direct_answer_detected" in validation.issues


def test_image_answer_validator_requires_uncertainty_disclaimer():
    invalid = filter_service.validate_image_answer("先看图中的已知条件，再判断受力方向。")
    valid = filter_service.validate_image_answer("我是 AI，可能会看错图片，我们一起讨论探索。先看图中的已知条件，再判断受力方向。")

    assert not invalid.allowed
    assert "missing_image_ai_disclaimer" in invalid.issues
    assert valid.allowed
