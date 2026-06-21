from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.learning_profile import StudentErrorEvent, StudentSkillProfile


ERROR_TYPE_LABELS = {
    "数学": {
        "concept_confusion": "概念混淆",
        "rule_misuse": "规则误用",
        "reading_error": "审题错误",
        "strategy_error": "解题策略错误",
        "calculation_error": "计算错误",
        "careless_error": "粗心错误",
        "knowledge_gap": "知识漏洞",
        "word_problem_modeling": "应用题建模困难",
    }
}


class SubjectProfileService:
    supported_error_subjects = {"数学"}
    record_signals = ("记录", "保存", "加入", "写入")
    record_targets = ("错因", "错题", "错误", "档案")
    vocabulary_signals = ("词汇DNA", "词汇 DNA", "存入词汇", "记单词", "背单词", "复习词汇")
    material_signals = ("素材库", "存入素材", "保存素材", "加入素材")
    excluded_english_words = {
        "dna",
        "english",
        "word",
        "words",
        "vocabulary",
    }

    def is_record_request(self, text: str, *, subject: str) -> bool:
        if subject not in self.supported_error_subjects:
            return False
        normalized = text.strip()
        return any(signal in normalized for signal in self.record_signals) and any(
            target in normalized for target in self.record_targets
        )

    def is_vocabulary_request(self, text: str) -> bool:
        normalized = text.strip()
        return any(signal in normalized for signal in self.vocabulary_signals)

    def is_chinese_material_request(self, text: str) -> bool:
        normalized = text.strip()
        return any(signal in normalized for signal in self.material_signals)

    def record_error_event(
        self,
        db: Session,
        *,
        student_id: int,
        subject: str,
        evidence_text: str,
        conversation_id: int | None = None,
        message_id: int | None = None,
    ) -> StudentErrorEvent:
        if subject not in self.supported_error_subjects:
            raise ValueError(f"unsupported subject profile: {subject}")
        evidence = " ".join(evidence_text.split()).strip() or f"学生请求记录本轮{subject}错因，但未提供具体错因描述。"
        error_type = self.classify_error_type(subject, evidence)
        event = StudentErrorEvent(
            student_id=student_id,
            subject=subject,
            conversation_id=conversation_id,
            message_id=message_id,
            knowledge_point=self.infer_knowledge_point(subject, evidence),
            error_type=error_type,
            evidence_text=evidence[:1000],
            confidence=0.82,
        )
        db.add(event)
        db.flush()
        self._rebuild_error_profile(db, student_id=student_id, subject=subject)
        return event

    def classify_error_type(self, subject: str, evidence: str) -> str:
        if subject != "数学":
            raise ValueError(f"unsupported subject profile: {subject}")
        text = evidence.strip()
        if any(keyword in text for keyword in ("应用题", "数量关系", "设什么", "设x", "设 x", "列方程", "建模")):
            return "word_problem_modeling"
        if any(keyword in text for keyword in ("概念", "定义", "混淆", "不知道为什么", "原理")):
            return "concept_confusion"
        if any(keyword in text for keyword in ("公式", "定理", "法则", "规则", "套用")):
            return "rule_misuse"
        if any(keyword in text for keyword in ("看错", "审题", "条件", "问什么")):
            return "reading_error"
        if any(keyword in text for keyword in ("计算", "算错", "符号", "移项", "化简")):
            return "calculation_error"
        if any(keyword in text for keyword in ("粗心", "漏写", "漏掉")):
            return "careless_error"
        if any(keyword in text for keyword in ("方法", "思路", "策略", "不会入手")):
            return "strategy_error"
        return "knowledge_gap"

    def infer_knowledge_point(self, subject: str, evidence: str) -> str:
        if subject != "数学":
            raise ValueError(f"unsupported subject profile: {subject}")
        text = evidence.strip()
        if any(keyword in text for keyword in ("应用题", "数量关系", "设什么", "设x", "设 x", "列方程")):
            return "设元列方程"
        if any(keyword in text for keyword in ("函数", "单调", "图像")):
            return "函数"
        if any(keyword in text for keyword in ("几何", "三角形", "圆", "角")):
            return "几何证明"
        if any(keyword in text for keyword in ("概率", "排列", "组合")):
            return "概率统计"
        if any(keyword in text for keyword in ("方程", "不等式")):
            return "方程与不等式"
        return "数学基础知识"

    def profile_summary(self, db: Session, *, student_id: int, subject: str) -> dict[str, Any]:
        profile = db.scalar(
            select(StudentSkillProfile).where(
                StudentSkillProfile.student_id == student_id,
                StudentSkillProfile.subject == subject,
            )
        )
        if not profile:
            return self.empty_summary(subject)
        return self._normalize_summary(subject, profile.profile_json or {})

    def empty_summary(self, subject: str) -> dict[str, Any]:
        return {
            "subject": subject,
            "total_events": 0,
            "top_error_type": None,
            "top_error_label": None,
            "error_counts": {},
            "recent_weaknesses": [],
        }

    def add_english_vocabulary(self, db: Session, *, student_id: int, source_text: str) -> dict[str, Any]:
        words = self._extract_english_words(source_text)
        profile = self._profile_for_update(db, student_id=student_id, subject="英语")
        profile_json = dict(profile.profile_json or {})
        items = list(profile_json.get("vocabulary_items") or [])
        existing = {str(item.get("word") or "").lower() for item in items if isinstance(item, dict)}
        added: list[str] = []
        for word in words:
            key = word.lower()
            if key in existing:
                continue
            items.append(
                {
                    "word": key,
                    "source_text": source_text[:500],
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            existing.add(key)
            added.append(key)
        profile_json["subject"] = "英语"
        profile_json["vocabulary_items"] = items
        profile_json["vocabulary_count"] = len(items)
        profile.profile_json = profile_json
        db.add(profile)
        db.flush()
        return {"added": added, "total": len(items)}

    def add_chinese_material(self, db: Session, *, student_id: int, source_text: str) -> dict[str, Any]:
        material_text = self._extract_chinese_material_text(source_text)
        profile = self._profile_for_update(db, student_id=student_id, subject="语文")
        profile_json = dict(profile.profile_json or {})
        items = list(profile_json.get("material_items") or [])
        normalized = material_text.strip()
        if normalized and normalized not in {str(item.get("text") or "").strip() for item in items if isinstance(item, dict)}:
            items.append(
                {
                    "text": normalized[:800],
                    "tags": self._infer_chinese_material_tags(normalized),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        profile_json["subject"] = "语文"
        profile_json["material_items"] = items
        profile_json["material_count"] = len(items)
        profile.profile_json = profile_json
        db.add(profile)
        db.flush()
        return {"added_count": 1 if normalized else 0, "total": len(items), "tags": self._infer_chinese_material_tags(normalized)}

    def _profile_for_update(self, db: Session, *, student_id: int, subject: str) -> StudentSkillProfile:
        profile = db.scalar(
            select(StudentSkillProfile).where(
                StudentSkillProfile.student_id == student_id,
                StudentSkillProfile.subject == subject,
            )
        )
        if profile:
            return profile
        profile = StudentSkillProfile(student_id=student_id, subject=subject, profile_json={"subject": subject})
        db.add(profile)
        db.flush()
        return profile

    def _rebuild_error_profile(self, db: Session, *, student_id: int, subject: str) -> None:
        events = db.scalars(
            select(StudentErrorEvent)
            .where(StudentErrorEvent.student_id == student_id, StudentErrorEvent.subject == subject)
            .order_by(StudentErrorEvent.created_at.desc(), StudentErrorEvent.id.desc())
        ).all()
        counts = Counter(event.error_type for event in events)
        top_error_type = counts.most_common(1)[0][0] if counts else None
        recent_weaknesses: list[str] = []
        for event in events[:5]:
            if event.knowledge_point and event.knowledge_point not in recent_weaknesses:
                recent_weaknesses.append(event.knowledge_point)
        profile = self._profile_for_update(db, student_id=student_id, subject=subject)
        current = dict(profile.profile_json or {})
        current.update(
            {
                "subject": subject,
                "total_events": len(events),
                "top_error_type": top_error_type,
                "top_error_label": ERROR_TYPE_LABELS.get(subject, {}).get(top_error_type or "") if top_error_type else None,
                "error_counts": dict(counts),
                "recent_weaknesses": recent_weaknesses,
            }
        )
        profile.profile_json = current
        db.add(profile)
        db.flush()

    def _normalize_summary(self, subject: str, raw: dict[str, Any]) -> dict[str, Any]:
        summary = self.empty_summary(subject)
        summary.update({key: raw.get(key) for key in summary if key in raw})
        summary["subject"] = str(summary.get("subject") or subject)
        summary["total_events"] = int(summary.get("total_events") or 0)
        if not isinstance(summary.get("error_counts"), dict):
            summary["error_counts"] = {}
        if not isinstance(summary.get("recent_weaknesses"), list):
            summary["recent_weaknesses"] = []
        top_error_type = summary.get("top_error_type")
        if top_error_type and not summary.get("top_error_label"):
            summary["top_error_label"] = ERROR_TYPE_LABELS.get(subject, {}).get(str(top_error_type))
        return summary

    def _extract_english_words(self, source_text: str) -> list[str]:
        words: list[str] = []
        for raw in re.findall(r"[A-Za-z][A-Za-z-]{2,}", source_text):
            word = raw.strip("-").lower()
            if word in self.excluded_english_words:
                continue
            if word not in words:
                words.append(word)
        return words

    def _extract_chinese_material_text(self, source_text: str) -> str:
        text = source_text.strip()
        for marker in ("：", ":"):
            if marker in text:
                return text.split(marker, 1)[1].strip()
        return text

    def _infer_chinese_material_tags(self, text: str) -> list[str]:
        tags: list[str] = []
        tag_keywords = {
            "坚持": ("坚持", "不放弃", "毅力"),
            "成长": ("成长", "改变", "成熟"),
            "家国": ("家国", "国家", "民族"),
            "责任": ("责任", "担当"),
            "逆境": ("被贬", "挫折", "困境", "逆境"),
        }
        for tag, keywords in tag_keywords.items():
            if any(keyword in text for keyword in keywords):
                tags.append(tag)
        return tags or ["待归类"]


subject_profile_service = SubjectProfileService()
