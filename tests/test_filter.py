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


# ---- 分学科输出校验（subjects 作用域）----


def test_adversarial_output_cases_match_expectation():
    cases = json.loads(Path("tests/adversarial_output_cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 20
    for case in cases:
        validation = filter_service.validate_answer(case["answer"], subject=case["subject"])
        assert validation.allowed is case["allowed"], f"{case['note']}: {case['answer']}"


def test_subject_scoped_rules_skipped_when_subject_is_none():
    # 旧调用方（不传 subject）行为零变化：学科作用域规则不触发
    math_leak = "答案是 x=4，把它代回去验证一下。"
    assert filter_service.validate_answer(math_leak).allowed
    assert not filter_service.validate_answer(math_leak, subject="数学").allowed
    # 全学科规则不受 subject 影响
    assert not filter_service.validate_answer("最终答案是 B。").allowed
    assert not filter_service.validate_answer("最终答案是 B。", subject="数学").allowed


def test_practice_exemption_covers_subject_scoped_rules():
    # 练习判卷豁免整个 direct_answer 层，新增学科规则同层自动被豁免
    validation = filter_service.validate_answer(
        "正确答案是C。答案是 x=4，你的解法在第二步出了问题。",
        skip_direct_answer=True,
        subject="数学",
    )
    assert validation.allowed


def test_chat_router_passes_subject_to_output_validation():
    source = Path("backend/routers/chat.py").read_text()

    assert source.count(
        "validate_answer(candidate_text, skip_direct_answer=practice_review_turn, subject=subject)"
    ) == 2
