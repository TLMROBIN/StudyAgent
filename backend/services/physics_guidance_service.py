from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.models.conversation import GuidanceStage


class DiagramType(StrEnum):
    FORCE = "force_diagram"
    MOTION_PROCESS = "motion_process"
    EQUIVALENT_CIRCUIT = "equivalent_circuit"
    LIGHT_PATH = "light_path"
    ENERGY_FLOW = "energy_flow"
    UNKNOWN = "unknown"


class ModelType(StrEnum):
    MECHANICAL_EQUILIBRIUM = "mechanical_equilibrium"
    UNIFORM_ACCELERATION = "uniform_acceleration"
    CIRCUIT_ANALYSIS = "circuit_analysis"
    ENERGY_CONSERVATION = "energy_conservation"
    COLLISION_MOMENTUM = "collision_momentum"
    UNKNOWN = "unknown"


class TeachingMode(StrEnum):
    PROBLEM_COACH = "problem_coach"
    MODELING_COACH = "modeling_coach"
    CONCEPT_INTUITION = "concept_intuition"


@dataclass(frozen=True)
class PhysicsGuidanceStrategy:
    diagram_type: DiagramType
    model_type: ModelType
    teaching_mode: TeachingMode
    prompt_section: str
    fallback_text: str


class PhysicsGuidanceService:
    practice_request_signals = (
        "再来一题",
        "再给我一题",
        "再给我一道",
        "出一道",
        "推荐一道",
        "推荐题",
        "巩固题",
        "练习题",
        "变式题",
        "同类型",
        "类似题",
    )

    concept_signals = ("什么是", "是什么意思", "为什么", "概念", "定义", "原理", "区别")

    def analyze(
        self,
        question: str,
        *,
        stage: GuidanceStage,
        image_summary: str | None = None,
    ) -> PhysicsGuidanceStrategy:
        text = f"{question} {image_summary or ''}"
        if self._looks_like_concept_question(question):
            return self._concept_strategy(stage)

        diagram_type = self._infer_diagram_type(text)
        model_type = self._infer_model_type(text, diagram_type)
        prompt_section = self._prompt_section(diagram_type, model_type)
        fallback_text = self._fallback_text(diagram_type, model_type, stage)
        return PhysicsGuidanceStrategy(
            diagram_type=diagram_type,
            model_type=model_type,
            teaching_mode=TeachingMode.PROBLEM_COACH
            if diagram_type != DiagramType.UNKNOWN
            else TeachingMode.MODELING_COACH,
            prompt_section=prompt_section,
            fallback_text=fallback_text,
        )

    def is_practice_request(self, question: str) -> bool:
        normalized = question.strip()
        return any(signal in normalized for signal in self.practice_request_signals)

    def _looks_like_concept_question(self, question: str) -> bool:
        return any(signal in question for signal in self.concept_signals) and not self.is_practice_request(question)

    def _infer_diagram_type(self, text: str) -> DiagramType:
        if any(keyword in text for keyword in ("电路", "电流", "电压", "电阻", "开关", "电流表", "电压表", "滑动变阻器")):
            return DiagramType.EQUIVALENT_CIRCUIT
        if any(keyword in text for keyword in ("光线", "反射", "折射", "透镜", "成像", "光路")):
            return DiagramType.LIGHT_PATH
        if any(keyword in text for keyword in ("能量", "动能", "势能", "机械能", "电能", "内能", "守恒", "转化")):
            return DiagramType.ENERGY_FLOW
        if any(keyword in text for keyword in ("力", "重力", "摩擦", "支持力", "拉力", "弹力", "斜面", "平衡", "匀速")):
            return DiagramType.FORCE
        if any(keyword in text for keyword in ("运动", "速度", "加速度", "减速", "位移", "追及", "自由落体")):
            return DiagramType.MOTION_PROCESS
        return DiagramType.UNKNOWN

    def _infer_model_type(self, text: str, diagram_type: DiagramType) -> ModelType:
        if diagram_type == DiagramType.EQUIVALENT_CIRCUIT:
            return ModelType.CIRCUIT_ANALYSIS
        if diagram_type == DiagramType.ENERGY_FLOW:
            return ModelType.ENERGY_CONSERVATION
        if any(keyword in text for keyword in ("碰撞", "撞击", "动量")):
            return ModelType.COLLISION_MOMENTUM
        if any(keyword in text for keyword in ("静止", "匀速", "平衡", "合力为零")):
            return ModelType.MECHANICAL_EQUILIBRIUM
        if diagram_type == DiagramType.MOTION_PROCESS:
            return ModelType.UNIFORM_ACCELERATION
        if diagram_type == DiagramType.FORCE:
            return ModelType.MECHANICAL_EQUILIBRIUM
        return ModelType.UNKNOWN

    def _concept_strategy(self, stage: GuidanceStage) -> PhysicsGuidanceStrategy:
        if stage == GuidanceStage.INITIAL:
            fallback = "先别急着背定义。你先想一个生活经验：类似现象你在哪里见过？我们再只改变一个条件，看看结果会怎样。"
        elif stage == GuidanceStage.HINT:
            fallback = "把刚才的生活经验变成一个小实验：只改变一个量，观察另一个量怎么变。你先说出变化方向，再把它对应到物理概念。"
        else:
            fallback = "我们按“生活经验→实验想象→公式意义”收束：先说经验，再说变量关系，最后把公式里的每个字母对应回真实情境。"
        return PhysicsGuidanceStrategy(
            diagram_type=DiagramType.UNKNOWN,
            model_type=ModelType.UNKNOWN,
            teaching_mode=TeachingMode.CONCEPT_INTUITION,
            prompt_section=(
                "物理概念直觉模式：先从学生熟悉的生活经验切入，再设计只改变一个变量的实验想象，"
                "最后才引出公式或定义；不要用物理术语解释物理术语。"
            ),
            fallback_text=fallback,
        )

    def _prompt_section(self, diagram_type: DiagramType, model_type: ModelType) -> str:
        diagram_label = {
            DiagramType.FORCE: "受力分析图",
            DiagramType.MOTION_PROCESS: "运动过程图",
            DiagramType.EQUIVALENT_CIRCUIT: "等效电路图",
            DiagramType.LIGHT_PATH: "光路图",
            DiagramType.ENERGY_FLOW: "能量转化图",
            DiagramType.UNKNOWN: "物理图景",
        }[diagram_type]
        model_label = {
            ModelType.MECHANICAL_EQUILIBRIUM: "力学平衡模型",
            ModelType.UNIFORM_ACCELERATION: "匀变速运动模型",
            ModelType.CIRCUIT_ANALYSIS: "电路分析模型",
            ModelType.ENERGY_CONSERVATION: "能量守恒模型",
            ModelType.COLLISION_MOMENTUM: "碰撞动量模型",
            ModelType.UNKNOWN: "待学生识别的模型",
        }[model_type]
        return (
            "物理专项引导策略：必须图景优先。先要求学生画出"
            f"{diagram_label}，再追问“你看到了什么物理现象”，随后让学生判断是否属于{model_label}。"
            "不要一上来列公式；公式只能在图景和模型确认后出现。"
        )

    def _fallback_text(self, diagram_type: DiagramType, model_type: ModelType, stage: GuidanceStage) -> str:
        if diagram_type == DiagramType.EQUIVALENT_CIRCUIT:
            if stage == GuidanceStage.INITIAL:
                return "先别急着套欧姆定律。你先把电压表当断路、电流表当导线，画出等效电路图；从电源正极出发，电流有几条路径？"
            if stage == GuidanceStage.HINT:
                return "把每个开关闭合/断开的状态分别画一张等效电路图，再判断串并联关系。你先完成其中一个状态。"
            return "我们按电路分析走：画等效电路图，标电流方向，判断串并联，再列关系式。最后一个数值结果请你自己算并检查单位。"
        if diagram_type == DiagramType.FORCE:
            if stage == GuidanceStage.INITIAL:
                return "先不要急着列公式。请先画受力分析图：选研究对象，标出重力、接触力、摩擦力或拉力分别在哪里。"
            if stage == GuidanceStage.HINT:
                return "检查你的受力图：物体和哪些面或绳子接触？如果是静止或匀速，水平方向和竖直方向分别应该怎样平衡？"
            return "我们把过程拆开：先画完整受力分析图，再判断合力是否为零，接着列平衡关系。最后一步代数计算和单位检查留给你完成。"
        if diagram_type == DiagramType.MOTION_PROCESS:
            return "先画运动过程图，标出初态、末态、速度方向和可能的加速度方向。你先判断这是不是匀变速直线运动。"
        if diagram_type == DiagramType.ENERGY_FLOW:
            return "先画能量转化图：初态有哪些能量，末态有哪些能量，中间有没有摩擦或热损失？你先列出能量去向。"
        if model_type == ModelType.COLLISION_MOMENTUM:
            return "先分清碰撞前后两个状态，并确定研究系统。你先判断外力能不能忽略，再考虑是否用动量守恒。"
        return "先用自己的话描述这道题的物理现象，再画一张图景草图。你看到了哪些物体、它们在做什么、哪些量发生了变化？"


physics_guidance_service = PhysicsGuidanceService()
