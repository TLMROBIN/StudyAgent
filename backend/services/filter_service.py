"""三层过滤服务（输入黑名单 / 学科白名单 / LLM 输出校验）。

规则本体由 backend/services/filter_rule_engine.py 提供：默认从
backend/data/filter_rules.json 加载，配置缺失/损坏时回退到代码内置
默认规则（fail-closed）。本模块只保留过滤流程与结构性校验逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

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
    rule_id: str | None = None


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

    _basic_math_pattern = re.compile(r"^[\d\s+\-×xX*÷/().（）=等于多少几？?]+$")
    _contextual_followup_pattern = re.compile(
        r"^(?:不知道|不会|没懂|没看懂|再来一次|继续|然后呢|"
        r"(?:第?[一二三四五六七八九十\d]+步|这一步|这里|这个|那个|为什么|为何|怎么|咋|哪).{0,36})$"
    )
    _academic_intent_pattern = re.compile(r"题|练习|巩固|错因|素材库|词汇|是什么|是否|溶于|定律|公式|概念|计算|证明|答案")
    _answer_attempt_pattern = re.compile(r"^(?:选|答案是?)?[A-Da-d][。.!！]?$")

    @staticmethod
    def normalize_text(text: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()

    def is_contextual_followup(self, normalized: str) -> bool:
        """Match an already normalized short turn or an explicit follow-up phrase."""
        return bool(normalized) and (
            len(normalized) <= 12 or bool(self._contextual_followup_pattern.fullmatch(normalized))
        )

    def check_question(
        self,
        question: str,
        declared_subject: str | None = None,
        *,
        allow_contextual_short_followup: bool = False,
    ) -> FilterDecision:
        snapshot = self._engine.snapshot()
        normalized = self.normalize_text(question)
        if not normalized:
            return FilterDecision(False, "empty_question")
        rule_id = self._blocking_rule(question, declared_subject, snapshot)
        if rule_id:
            return FilterDecision(False, "blocked_non_subject", rule_id=rule_id)

        # 规则只做初筛；*_candidate 必须由调用方再通过实际话题分类。
        selected_subject = declared_subject if declared_subject in snapshot.subjects else None
        if allow_contextual_short_followup and self.is_contextual_followup(normalized):
            return FilterDecision(True, "academic_context_followup", selected_subject)
        if self._basic_math_pattern.fullmatch(normalized) and any(token in normalized for token in "+-×x*/÷/=等于多少几"):
            return FilterDecision(True, "basic_math_expression", selected_subject or "数学")
        if selected_subject and self._answer_attempt_pattern.fullmatch(normalized):
            return FilterDecision(True, "answer_attempt", selected_subject)

        for subject, keywords in snapshot.subjects.items():
            if any(self.normalize_text(keyword) in normalized for keyword in keywords):
                return FilterDecision(True, "keyword_candidate", selected_subject or subject)
        if len(normalized) >= snapshot.generic_min_length and any(token in normalized for token in snapshot.generic_tokens):
            return FilterDecision(True, "generic_academic_candidate", selected_subject)
        if selected_subject and self._academic_intent_pattern.search(normalized):
            return FilterDecision(True, "academic_intent_candidate", selected_subject)

        return FilterDecision(False, "subject_not_recognized")

    def is_question_blocked(self, text: str, subject: str | None = None) -> bool:
        return self._blocking_rule(text, subject, self._engine.snapshot()) is not None

    @staticmethod
    def _blocking_rule(text: str, subject: str | None, snapshot: RuleSnapshot) -> str | None:
        spaced = unicodedata.normalize("NFKC", text or "").casefold()
        compact = re.sub(r"\s+", "", spaced)
        for rule in snapshot.layer_rules(LAYER_QUESTION_BLOCKLIST):
            if rule.subjects and subject not in rule.subjects:
                continue
            if rule.pattern.search(compact) or rule.pattern.search(spaced):
                return rule.id
        return None

    # ---- LLM 输出校验 ----

    def validate_answer(
        self,
        answer: str,
        *,
        skip_direct_answer: bool = False,
        subject: str | None = None,
        question: str | None = None,
    ) -> OutputValidation:
        """输出校验。

        subject 语义：规则 subjects 为空 → 全学科生效（原行为）；
        subjects 非空 → 仅当 subject 命中时生效。subject=None（旧调用方）
        时跳过所有 subject-scoped 规则，存量行为零变化。
        """
        snapshot = self._engine.snapshot()
        issues: list[str] = []
        if question and self.is_question_blocked(question, subject):
            issues.append("blocked_non_subject_request")
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
