from backend.models.conversation import GuidanceStage
from backend.services.physics_guidance_service import (
    DiagramType,
    ModelType,
    TeachingMode,
    physics_guidance_service,
)


def test_physics_guidance_identifies_force_diagram_and_equilibrium_model():
    strategy = physics_guidance_service.analyze(
        "如图所示，物块在水平面上匀速运动，求摩擦力大小",
        stage=GuidanceStage.INITIAL,
    )

    assert strategy.diagram_type == DiagramType.FORCE
    assert strategy.model_type == ModelType.MECHANICAL_EQUILIBRIUM
    assert strategy.teaching_mode == TeachingMode.PROBLEM_COACH
    assert "受力分析图" in strategy.prompt_section
    assert "先画" in strategy.fallback_text


def test_physics_guidance_identifies_circuit_image_summary():
    strategy = physics_guidance_service.analyze(
        "请看图分析",
        stage=GuidanceStage.INITIAL,
        image_summary="图片中有电源、开关、电流表、电压表和滑动变阻器。",
    )

    assert strategy.diagram_type == DiagramType.EQUIVALENT_CIRCUIT
    assert strategy.model_type == ModelType.CIRCUIT_ANALYSIS
    assert "等效电路图" in strategy.prompt_section
    assert "电压表当断路" in strategy.fallback_text


def test_physics_guidance_uses_concept_intuition_for_why_questions():
    strategy = physics_guidance_service.analyze(
        "为什么电压会推动电流？",
        stage=GuidanceStage.INITIAL,
    )

    assert strategy.teaching_mode == TeachingMode.CONCEPT_INTUITION
    assert "生活经验" in strategy.prompt_section
    assert "实验想象" in strategy.prompt_section
    assert "公式" in strategy.prompt_section


def test_physics_guidance_detects_explicit_practice_request():
    assert physics_guidance_service.is_practice_request("再给我一道同类型巩固题")
    assert physics_guidance_service.is_practice_request("推荐一道受力分析练习")
    assert not physics_guidance_service.is_practice_request("这道题为什么要画受力图")
