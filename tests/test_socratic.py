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


def test_build_prompt_does_not_force_disclaimer_for_high_confidence_image_turn():
    prompt = socratic_service.build_prompt(
        question="请看这张图",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
        image_summary="图中给出了受力分析示意。",
        image_confidence="high",
        image_related=True,
    )
    system_text = prompt.messages[0]["content"]

    assert "每一轮回复都必须明确说明" not in system_text
    assert "看错图片" not in system_text


def test_build_prompt_requires_grounding_on_image_summary():
    prompt = socratic_service.build_prompt(
        question="请看图",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
        image_summary="匀强电场、匀强磁场、带电微粒沿直线运动。",
        image_confidence="high",
        image_related=True,
    )
    system_text = prompt.messages[0]["content"]

    assert "必须先引用图片理解摘要中的1-2个具体关键词" in system_text
    assert "匀强电场、匀强磁场、带电微粒沿直线运动" in system_text


def test_basic_concept_question_uses_explanation_mode():
    prompt = socratic_service.build_prompt(
        question="什么是惯性",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "问题类型：concept_explanation" in system_text
    assert "2-4 句解释基础概念" in system_text
    assert "1 个检查理解的问题" in system_text


def test_exercise_question_stays_guided_even_with_answer_language():
    prompt = socratic_service.build_prompt(
        question="这道题答案是多少",
        subject="数学",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "问题类型：calculation" in system_text
    assert "基础知识解释模式" not in system_text


def test_image_low_confidence_text_asks_user_to_correct_partial_understanding():
    text = socratic_service.image_low_confidence_text("数学", image_summary="像是一道函数图像题。")

    assert "看得不太清" in text
    assert "理解可能有误" in text
    assert "像是一道函数图像题" in text
    assert "纠正" in text


def test_image_extremely_low_confidence_text_reports_recognition_failure():
    text = socratic_service.image_low_confidence_text("数学")

    assert "识别失败" in text
    assert "重新上传" in text
