from __future__ import annotations

import json
import re
from typing import Any

from backend.models.conversation import GuidanceStage
from backend.services.filter_service import filter_service
from backend.services.llm_service import LLMService, llm_service


MIN_REPLY_COUNT = 3
MAX_REPLY_COUNT = 5
MAX_REPLY_CHARS = 64
MAX_CONTEXT_CHARS = 1600
MAX_ASSISTANT_CHARS = 1200
GENERIC_REPLIES = {
    "继续",
    "不知道",
    "我不知道",
    "不会",
    "我不会",
    "再讲讲",
    "讲详细点",
}
STUDENT_REPLY_DIRECT_PATTERNS = (
    re.compile(r"(最终|标准)?答案(?:是|为|选|：|:)"),
    re.compile(r"(正确答案|正确选项)"),
    re.compile(r"(完整步骤|完整解法|标准解)"),
    re.compile(r"(?:应该|应当|所以)?选\s*[（(]?[A-D](?![A-Za-z])"),
)
GENERIC_FALLBACK_REPLIES = (
    "我先说一个关键词，你帮我判断方向对不对。",
    "是不是还需要补充一个适用条件？",
    "我不确定下一步该看哪个条件。",
    "能不能给我一个更小的提示？",
)
COULOMB_FALLBACK_REPLIES = (
    "两个带电物体之间吗？",
    "是不是还要看能不能近似成点电荷？",
    "我不确定距离这个条件要怎么说。",
)


class SuggestedReplyService:
    def __init__(self, llm: LLMService | None = None) -> None:
        self.llm = llm or llm_service

    async def generate(
        self,
        *,
        subject: str,
        guidance_stage: GuidanceStage,
        current_question: str,
        assistant_response: str,
        history: list[tuple[str, str]],
        model_key: str | None = None,
    ) -> list[str]:
        if not self._should_offer(assistant_response):
            return []

        messages = self._build_messages(
            subject=subject,
            guidance_stage=guidance_stage,
            current_question=current_question,
            assistant_response=assistant_response,
            history=history,
        )
        raw_text = await self.llm.complete_response(
            messages,
            "",
            model_key=model_key,
            max_completion_tokens=220,
            temperature=0.35,
        )
        replies = self._normalize_options(raw_text, subject=subject)
        if len(replies) < MIN_REPLY_COUNT:
            replies = self._supplement_options(
                replies,
                subject=subject,
                current_question=current_question,
                assistant_response=assistant_response,
                history=history,
            )
        return replies[:MAX_REPLY_COUNT] if len(replies) >= MIN_REPLY_COUNT else []

    def _build_messages(
        self,
        *,
        subject: str,
        guidance_stage: GuidanceStage,
        current_question: str,
        assistant_response: str,
        history: list[tuple[str, str]],
    ) -> list[dict[str, object]]:
        history_text = self._format_history(history)
        current_question = self._clip(current_question, MAX_CONTEXT_CHARS)
        assistant_response = self._clip(assistant_response, MAX_ASSISTANT_CHARS)
        system_prompt = (
            "你为高中学科 AI 导师生成学生可点击的下一轮回复选项。"
            "这些选项必须像真实学生会说的话，能自然回应导师刚刚的问题或提示。"
            "只输出 JSON，不输出解释、编号、Markdown 或思考过程。"
            "JSON 格式固定为：{\"replies\":[\"...\",\"...\",\"...\"]}。"
            "规则：生成 3 到 5 条；每条 6 到 64 个中文字符左右；"
            "必须具有思辨性，可以表达猜想、困惑、条件判断、切入角度或请求更具体支架；"
            "禁止给最终答案、标准答案、完整步骤、选项结论或替学生完成最后一步；"
            "禁止闲聊、套话和生硬的“继续/不知道/再讲讲”。"
            "如果当前回复不适合生成学生选项，输出 {\"replies\":[]}。"
        )
        user_prompt = (
            f"学科：{subject}\n"
            f"引导阶段：{guidance_stage.value}\n"
            f"最近历史：\n{history_text or '（无）'}\n\n"
            f"学生本轮问题：{current_question or '（无）'}\n\n"
            f"导师刚刚回复：\n{assistant_response}\n\n"
            "请生成学生下一步可能点击发送的回复选项。"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _format_history(self, history: list[tuple[str, str]]) -> str:
        lines: list[str] = []
        for role, content in history[-6:]:
            clipped = self._clip(content, 180)
            if clipped:
                lines.append(f"{role}: {clipped}")
        return self._clip("\n".join(lines), MAX_CONTEXT_CHARS)

    def _normalize_options(self, raw_text: str, *, subject: str | None = None) -> list[str]:
        payload = self._parse_json(raw_text)
        candidates = self._candidate_strings(payload)
        return self._filter_options(candidates, subject=subject)

    def _filter_options(self, candidates: list[str], *, subject: str | None = None) -> list[str]:
        replies: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = self._clean_reply(candidate)
            if not normalized or normalized in seen:
                continue
            if not self._is_allowed_reply(normalized, subject=subject):
                continue
            replies.append(normalized)
            seen.add(normalized)
            if len(replies) >= MAX_REPLY_COUNT:
                break
        return replies

    def _supplement_options(
        self,
        replies: list[str],
        *,
        subject: str,
        current_question: str,
        assistant_response: str,
        history: list[tuple[str, str]],
    ) -> list[str]:
        context_text = "\n".join(
            [
                current_question or "",
                assistant_response or "",
                self._format_history(history),
            ]
        )
        fallback_candidates: list[str] = []
        if subject == "物理" and any(keyword in context_text for keyword in ("库仑定律", "库仑力", "点电荷")):
            fallback_candidates.extend(COULOMB_FALLBACK_REPLIES)
        fallback_candidates.extend(GENERIC_FALLBACK_REPLIES)

        combined = list(replies)
        seen = set(combined)
        for candidate in fallback_candidates:
            normalized = self._clean_reply(candidate)
            if not normalized or normalized in seen:
                continue
            if not self._is_allowed_reply(normalized, subject=subject):
                continue
            combined.append(normalized)
            seen.add(normalized)
            if len(combined) >= MIN_REPLY_COUNT:
                break
        return combined

    def _parse_json(self, raw_text: str) -> Any:
        text = self._strip_thinking(raw_text).strip()
        if not text:
            return None
        candidates = [text]
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                candidates.append(text[start : end + 1])
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate.startswith("```"):
                candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
                candidate = re.sub(r"\s*```$", "", candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _candidate_strings(payload: Any) -> list[str]:
        if isinstance(payload, dict):
            for key in ("replies", "options", "suggested_replies"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [str(item) for item in value]
        if isinstance(payload, list):
            return [str(item) for item in payload]
        return []

    @staticmethod
    def _clean_reply(value: str) -> str:
        text = re.sub(r"\s+", " ", value).strip()
        text = re.sub(r"^[\d一二三四五六七八九十]+[.、．)]\s*", "", text)
        return text.strip("「」“”\"'` ")

    @staticmethod
    def _strip_thinking(text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", (text or "").strip())
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit].rstrip()

    @staticmethod
    def _should_offer(assistant_response: str) -> bool:
        text = (assistant_response or "").strip()
        if not text:
            return False
        blocked_prefixes = (
            filter_service.refusal_text,
            "已记录到",
            "已加入英语词汇 DNA",
            "已存入语文素材库",
        )
        return not any(text.startswith(prefix) for prefix in blocked_prefixes)

    @staticmethod
    def _is_allowed_reply(reply: str, *, subject: str | None = None) -> bool:
        if len(reply) < 3 or len(reply) > MAX_REPLY_CHARS:
            return False
        if reply in GENERIC_REPLIES:
            return False
        if any(pattern.search(reply) for pattern in STUDENT_REPLY_DIRECT_PATTERNS):
            return False
        if not filter_service.validate_answer(reply, subject=subject).allowed:
            return False
        return not filter_service.is_question_blocked(reply)


suggested_reply_service = SuggestedReplyService()
