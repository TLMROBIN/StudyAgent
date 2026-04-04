from backend.models.conversation import GuidanceStage
from backend.services.socratic_service import socratic_service


def test_guidance_stage_progression():
    assert socratic_service.infer_stage(0) == GuidanceStage.INITIAL
    assert socratic_service.infer_stage(1) == GuidanceStage.HINT
    assert socratic_service.infer_stage(3) == GuidanceStage.FALLBACK


def test_fallback_text_never_contains_final_answer_phrase():
    text = socratic_service.build_fallback_text("求函数最值", "数学", GuidanceStage.FALLBACK, "calculation")
    assert "最终答案" not in text
    assert "标准答案" not in text


def test_build_prompt_adds_latex_instruction_for_stem_subjects():
    prompt = socratic_service.build_prompt(
        question="已知加速度 a 和时间 t，求位移",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
        student_grade=2,
    )
    system_text = prompt.messages[0]["content"]

    assert "标准 LaTeX" in system_text
    assert "$...$" in system_text
    assert "$$...$$" in system_text
