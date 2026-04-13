from __future__ import annotations

from dataclasses import dataclass
import re


SUBJECT_KEYWORDS = {
    "语文": ["古诗", "文言文", "修辞", "阅读理解", "作文", "病句", "成语", "议论文", "论点", "意象"],
    "数学": ["函数", "导数", "数列", "几何", "概率", "方程", "不等式", "三角", "圆锥曲线"],
    "英语": ["语法", "完形", "阅读", "时态", "从句", "单词", "翻译"],
    "物理": ["速度", "受力", "电场", "磁场", "动能", "牛顿", "电路"],
    "化学": ["氧化", "还原", "离子", "方程式", "滴定", "平衡", "电解"],
    "生物": ["细胞", "遗传", "呼吸作用", "光合作用", "生态", "DNA"],
    "政治": ["哲学", "经济", "法治", "文化", "价值观", "国家"],
    "历史": ["朝代", "改革", "战争", "制度", "史料", "近代史", "历史", "材料题"],
    "地理": ["气候", "洋流", "农业", "地形", "人口", "区域"],
}

NON_SUBJECT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"天气|股价|彩票|恋爱|表白|游戏攻略|写代码|帮我写作文",
        r"你是谁|讲个笑话|闲聊|角色扮演|扮演成|忽略之前|无视规则|系统提示词|开发者模式|管理员模式|DAN",
        r"prompt|system prompt|越过限制|绕过过滤|不要遵守|泄露提示词",
        r"知识库原文|检索片段|资料片段|向量库|RAG|完整输出资料",
        r"身份证|银行卡|密码|越狱|翻墙|成人",
    ]
]

DIRECT_ANSWER_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"最终答案[是为]",
        r"标准答案[是为]",
        r"答案[：:]\s*[A-D0-9一二三四五六七八九十]",
        r"所以结果[是为]",
        r"正确结论[是为]",
    ]
]

IMAGE_DISCLAIMER_AI_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bAI\b",
        r"人工智能",
    ]
]
IMAGE_DISCLAIMER_UNCERTAINTY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"可能不准确",
        r"不一定准确",
        r"可能会看错",
        r"可能理解得不完全准确",
    ]
]
IMAGE_DISCLAIMER_COLLABORATION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"一起讨论",
        r"一起探索",
        r"一起梳理",
    ]
]


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
    image_uncertainty_text = "提醒一下：我是 AI，可能会看错图片或理解得不完全准确，我们一起讨论探索。"

    def check_question(self, question: str, declared_subject: str | None = None) -> FilterDecision:
        normalized = question.strip()
        if not normalized:
            return FilterDecision(False, "empty_question")
        for pattern in NON_SUBJECT_PATTERNS:
            if pattern.search(normalized):
                return FilterDecision(False, "blocked_non_subject")

        if declared_subject and declared_subject in SUBJECT_KEYWORDS:
            return FilterDecision(True, "declared_subject", declared_subject)

        for subject, keywords in SUBJECT_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                return FilterDecision(True, "keyword_match", subject)

        if len(normalized) >= 6 and any(token in normalized for token in ["为什么", "如何", "怎么", "求", "分析", "解释", "评价", "概括"]):
            return FilterDecision(True, "generic_academic_pattern", declared_subject)

        return FilterDecision(False, "subject_not_recognized")

    def validate_answer(self, answer: str) -> OutputValidation:
        issues: list[str] = []
        if any(pattern.search(answer) for pattern in DIRECT_ANSWER_PATTERNS):
            issues.append("direct_answer_detected")
        if "抱歉，我只能解答高中学科相关问题" in answer and len(answer) > 30:
            issues.append("mixed_refusal")
        return OutputValidation(allowed=not issues, issues=issues)

    def validate_image_answer(self, answer: str) -> OutputValidation:
        issues = list(self.validate_answer(answer).issues)
        if not any(pattern.search(answer) for pattern in IMAGE_DISCLAIMER_AI_PATTERNS):
            issues.append("missing_image_ai_disclaimer")
        if not any(pattern.search(answer) for pattern in IMAGE_DISCLAIMER_UNCERTAINTY_PATTERNS):
            issues.append("missing_image_uncertainty_disclaimer")
        if not any(pattern.search(answer) for pattern in IMAGE_DISCLAIMER_COLLABORATION_PATTERNS):
            issues.append("missing_image_collaboration_disclaimer")
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
