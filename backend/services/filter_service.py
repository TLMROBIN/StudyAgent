"""三层过滤服务（输入黑名单 / 学科白名单 / LLM 输出校验）。

规则本体由 backend/services/filter_rule_engine.py 提供：默认从
backend/data/filter_rules.json 加载，配置缺失/损坏时回退到代码内置
默认规则（fail-closed）。本模块只保留过滤流程与结构性校验逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.services.filter_rule_engine import (
    LAYER_DIRECT_ANSWER,
    LAYER_IMAGE_UNCERTAINTY,
    LAYER_QUESTION_BLOCKLIST,
    FilterRuleEngine,
    RuleSnapshot,
    default_engine,
)


@dataclass
class FilterDecision:
    allowed: bool
    reason: str
    subject: str | None = None


@dataclass
class OutputValidation:
    allowed: bool
    issues: list[str]


class FilterService:
    refusal_text = "抱歉，我只能解答高中学科相关问题，请重新提问。"
    image_uncertainty_text = "这张图片我看得不太清，理解可能有误。请你帮我核对一下。"

    def __init__(self, engine: FilterRuleEngine | None = None) -> None:
        self._engine = engine if engine is not None else default_engine

    # ---- 规则管理（供管理端点/运维调用）----

    def reload_rules(self) -> dict[str, object]:
        """强制重载规则配置，返回 JSON 可序列化的状态摘要。"""
        return self._rules_payload(self._engine.reload())

    def rules_status(self) -> dict[str, object]:
        return self._rules_payload(self._engine.snapshot())

    @staticmethod
    def _rules_payload(snapshot: RuleSnapshot) -> dict[str, object]:
        return {
            "source": snapshot.source,
            "config_path": snapshot.config_path,
            "error": snapshot.error,
            "loaded_at": snapshot.loaded_at,
            "enabled_rules": snapshot.enabled_rule_count,
            "disabled_rule_ids": list(snapshot.disabled_rule_ids),
        }

    # ---- 输入过滤（黑名单 + 学科白名单）----

    def check_question(self, question: str, declared_subject: str | None = None) -> FilterDecision:
        snapshot = self._engine.snapshot()
        normalized = question.strip()
        if not normalized:
            return FilterDecision(False, "empty_question")
        for rule in snapshot.layer_rules(LAYER_QUESTION_BLOCKLIST):
            if rule.pattern.search(normalized):
                return FilterDecision(False, "blocked_non_subject")

        if declared_subject and declared_subject in snapshot.subjects:
            return FilterDecision(True, "declared_subject", declared_subject)

        for subject, keywords in snapshot.subjects.items():
            if any(keyword in normalized for keyword in keywords):
                return FilterDecision(True, "keyword_match", subject)

        if len(normalized) >= snapshot.generic_min_length and any(token in normalized for token in snapshot.generic_tokens):
            return FilterDecision(True, "generic_academic_pattern", declared_subject)

        return FilterDecision(False, "subject_not_recognized")

    def is_question_blocked(self, text: str) -> bool:
        snapshot = self._engine.snapshot()
        normalized = text.strip()
        return any(rule.pattern.search(normalized) for rule in snapshot.layer_rules(LAYER_QUESTION_BLOCKLIST))

    # ---- LLM 输出校验 ----

    def validate_answer(
        self,
        answer: str,
        *,
        skip_direct_answer: bool = False,
        subject: str | None = None,
    ) -> OutputValidation:
        """输出校验。

        subject 语义：规则 subjects 为空 → 全学科生效（原行为）；
        subjects 非空 → 仅当 subject 命中时生效。subject=None（旧调用方）
        时跳过所有 subject-scoped 规则，存量行为零变化。
        """
        snapshot = self._engine.snapshot()
        issues: list[str] = []
        if not skip_direct_answer:
            for rule in snapshot.layer_rules(LAYER_DIRECT_ANSWER):
                if rule.subjects and (subject is None or subject not in rule.subjects):
                    continue
                if rule.pattern.search(answer):
                    issues.append("direct_answer_detected")
                    break
        # 结构性校验（拒答文案与正文混排），依赖长度组合判断，保留在代码内
        if "抱歉，我只能解答高中学科相关问题" in answer and len(answer) > 30:
            issues.append("mixed_refusal")
        return OutputValidation(allowed=not issues, issues=issues)

    def validate_image_answer(self, answer: str) -> OutputValidation:
        snapshot = self._engine.snapshot()
        issues = list(self.validate_answer(answer).issues)
        if not any(rule.pattern.search(answer) for rule in snapshot.layer_rules(LAYER_IMAGE_UNCERTAINTY)):
            issues.append("missing_image_uncertainty_disclaimer")
        return OutputValidation(allowed=not issues, issues=issues)

    def ensure_image_disclaimer(self, answer: str) -> str:
        validation = self.validate_image_answer(answer)
        if validation.allowed:
            return answer.strip()
        cleaned = answer.strip()
        if not cleaned:
            return self.image_uncertainty_text
        return f"{self.image_uncertainty_text}\n\n{cleaned}"


filter_service = FilterService()
