from __future__ import annotations

from dataclasses import dataclass

from backend.grade_utils import format_grade_label
from backend.models.conversation import GuidanceStage, IMAGE_ONLY_MESSAGE_PLACEHOLDER
from backend.services.filter_service import filter_service


@dataclass
class PromptPackage:
    messages: list[dict[str, str]]
    stage: GuidanceStage
    fallback_text: str
    image_related: bool = False


class SocraticService:
    base_prompt = (
        "你是一位高中学科答疑导师，采用苏格拉底助产术。"
        "你必须优先用问题引导学生思考，不直接给出最终结论或标准答案。"
        "如学生多轮卡住，可以分步解析，但最后一步要留给学生自己完成。"
        "只回答高中语文、数学、英语、物理、化学、生物、政治、历史、地理。"
    )

    def infer_stage(self, turn_count: int) -> GuidanceStage:
        if turn_count >= 3:
            return GuidanceStage.FALLBACK
        if turn_count >= 1:
            return GuidanceStage.HINT
        return GuidanceStage.INITIAL

    def infer_question_type(self, question: str) -> str:
        lowered = question.strip()
        exercise_signals = ["求", "计算", "证明", "推导", "答案", "选项", "如图", "第", "解方程", "解不等式"]
        concept_signals = ["什么是", "是什么意思", "区别", "概念", "定义", "原理", "为什么"]
        if any(keyword in lowered for keyword in exercise_signals):
            return "calculation"
        if any(keyword in lowered for keyword in ["分析", "评价", "说明原因", "材料"]):
            return "analysis"
        if any(keyword in lowered for keyword in concept_signals):
            return "concept_explanation"
        return "concept"

    def build_prompt(
        self,
        question: str,
        subject: str,
        history: list[tuple[str, str]],
        retrieved_context: str,
        system_prompt: str,
        student_grade: int | None = None,
        image_summary: str | None = None,
        image_confidence: str | None = None,
        image_related: bool = False,
    ) -> PromptPackage:
        turn_count = len(history) // 2
        stage = self.infer_stage(turn_count)
        question_type = self.infer_question_type(question)
        system_sections = [
            self.base_prompt,
            system_prompt,
            f"当前学科：{subject}",
            f"当前引导阶段：{stage.value}",
            f"问题类型：{question_type}",
            "请保持语气平和，先用问题推进理解，再给必要提示。",
            "涉及数学、物理、化学中的公式、方程、上下标或希腊字母时，请使用标准 LaTeX 书写。",
            "行内公式使用 $...$，独立公式使用 $$...$$，不要使用图片或伪公式文本代替。",
        ]
        if question_type == "concept_explanation":
            system_sections.append(
                "基础知识解释模式：先用 2-4 句解释基础概念，再问 1 个检查理解的问题。"
                "不要代写题目最终答案；如果问题其实是具体习题，转为引导式提问。"
            )
        if image_related:
            system_sections.append(
                "本轮依赖图片理解作答。你的每一轮回复都必须明确说明：你是 AI，可能会看错图片或理解得不完全准确，需要和学生一起讨论探索。"
            )
            system_sections.append("如果图片信息不充分或不可靠，必须直说看不准，并先引导学生补充更清晰图片或文字。")
        if student_grade is not None:
            system_sections.append(f"当前学生年级：{format_grade_label(student_grade) or f'{student_grade}年级'}")
        if image_summary:
            system_sections.append(f"图片理解摘要：{image_summary}")
        if image_confidence:
            system_sections.append(f"图片理解置信度：{image_confidence}")
        if retrieved_context:
            system_sections.append(f"知识库参考：{retrieved_context}")

        messages = [{"role": "system", "content": "\n".join(system_sections)}]
        for role, content in history[-6:]:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})
        return PromptPackage(
            messages=messages,
            stage=stage,
            fallback_text=self.build_fallback_text(question, subject, stage, question_type, image_related=image_related),
            image_related=image_related,
        )

    def build_fallback_text(
        self,
        question: str,
        subject: str,
        stage: GuidanceStage,
        question_type: str,
        *,
        image_related: bool = False,
    ) -> str:
        if question_type == "calculation":
            if stage == GuidanceStage.INITIAL:
                text = f"先不要急着算结果。针对这道{subject}题，你先把已知条件和要求的量分别列出来，第一步你打算从哪个公式或定理入手？"
                return self._wrap_image_text(text, image_related=image_related)
            if stage == GuidanceStage.HINT:
                text = f"可以先把题目拆成两步。先判断要用哪个核心公式，再想这个公式里哪些量已经已知、哪些量还需要先求。你先试着完成第一步。"
                return self._wrap_image_text(text, image_related=image_related)
            return self._wrap_image_text("我们把过程拆开：先确定已知量和目标量，再写出对应关系式，接着代入能确定的数据推进。最后一个结果请你自己算出来并检查单位或符号。", image_related=image_related)
        if question_type == "analysis":
            if stage == GuidanceStage.INITIAL:
                text = f"这个{subject}问题适合先搭框架。你觉得应该先从背景、核心概念，还是因果关系这三个角度中的哪一个切入？"
                return self._wrap_image_text(text, image_related=image_related)
            if stage == GuidanceStage.HINT:
                return self._wrap_image_text("可以先抓住题目中的关键词，再分别看它对应的概念、条件和结论之间是什么关系。你先说出你最确定的一点。", image_related=image_related)
            return self._wrap_image_text("先把材料或题干中的关键信息圈出来，再按“现象/条件/原因/影响”这条线整理，最后一步结论请你结合前面的分析自己补全。", image_related=image_related)
        if stage == GuidanceStage.INITIAL:
            return self._wrap_image_text(
                f"这是一个{subject}概念问题。你先试着说说题目里最核心的概念是什么意思，或者它和相近概念有什么区别？",
                image_related=image_related,
            )
        if stage == GuidanceStage.HINT:
            return self._wrap_image_text("可以先回忆定义，再找一个典型例子验证你的理解。如果两个概念容易混淆，就先比较它们的条件和结果。", image_related=image_related)
        return self._wrap_image_text("先把定义、适用条件和常见表现分别列出来，再用题目情境去对应它们。最后那个判断由你自己完成，我可以继续帮你检查思路。", image_related=image_related)

    def safe_guided_rewrite(self, question: str, subject: str, stage: GuidanceStage, *, image_related: bool = False) -> str:
        question_type = self.infer_question_type(question)
        return self.build_fallback_text(question, subject, stage, question_type, image_related=image_related)

    def image_low_confidence_text(self, subject: str) -> str:
        text = (
            f"这张{subject}题目的图片我暂时没有看清关键条件。"
            "你可以先试着重拍一张更清晰的图片，或者补充一句你最想问的地方。"
            "等信息更完整后，我们再一起一步步梳理解题思路。"
        )
        return self._wrap_image_text(text, image_related=True)

    @staticmethod
    def placeholder_question(subject: str) -> str:
        return f"{subject}{IMAGE_ONLY_MESSAGE_PLACEHOLDER}"

    def _wrap_image_text(self, text: str, *, image_related: bool) -> str:
        if not image_related:
            return text
        return filter_service.ensure_image_disclaimer(text)


socratic_service = SocraticService()
