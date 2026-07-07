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


def test_output_validator_practice_exemption_skips_direct_answer_only():
    direct = filter_service.validate_answer("最终答案是 B，因为代入后满足条件。", skip_direct_answer=True)
    mixed = filter_service.validate_answer("抱歉，我只能解答高中学科相关问题，但最终答案是 B，请直接照这个填写。", skip_direct_answer=True)

    assert direct.allowed
    assert not mixed.allowed
    assert mixed.issues == ["mixed_refusal"]


def test_output_validator_default_behavior_still_blocks_direct_answer():
    validation = filter_service.validate_answer("最终答案是 B，因为代入后满足条件。")

    assert not validation.allowed
    assert validation.issues == ["direct_answer_detected"]


def test_chinese_writing_coach_request_is_allowed_as_academic_prompt():
    decision = filter_service.check_question("帮我写作文怎么构思，不要代写", "语文")

    assert decision.allowed
    assert decision.subject == "语文"


def test_image_answer_validator_accepts_human_low_confidence_disclaimer():
    invalid = filter_service.validate_image_answer("先看图中的已知条件，再判断受力方向。")
    valid = filter_service.validate_image_answer("这张图片我看得不太清，理解可能有误。先看图中的已知条件，再判断受力方向。")

    assert not invalid.allowed
    assert "missing_image_uncertainty_disclaimer" in invalid.issues
    assert valid.allowed
