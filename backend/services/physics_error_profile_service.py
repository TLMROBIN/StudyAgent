from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.learning_profile import StudentErrorEvent, StudentSkillProfile


ERROR_TYPE_LABELS = {
    "diagram_establishment": "图景建立错误",
    "concept_confusion": "概念混淆",
    "formula_misuse": "公式误用",
    "process_analysis": "过程分析错误",
    "math_tool": "数学工具错误",
}


class PhysicsErrorProfileService:
    record_signals = ("记录", "保存", "加入", "写入")
    record_targets = ("错因", "错题", "错误", "档案")

    def is_record_request(self, text: str) -> bool:
        normalized = text.strip()
        return any(signal in normalized for signal in self.record_signals) and any(
            target in normalized for target in self.record_targets
        )

    def record_event(
        self,
        db: Session,
        *,
        student_id: int,
        subject: str,
        evidence_text: str,
        conversation_id: int | None = None,
        message_id: int | None = None,
    ) -> StudentErrorEvent:
        if subject != "物理":
            raise ValueError("physics error profile only supports physics")
        evidence = " ".join(evidence_text.split()).strip()
        if not evidence:
            evidence = "学生请求记录本轮物理错因，但未提供具体错因描述。"
        error_type = self.classify_error_type(evidence)
        event = StudentErrorEvent(
            student_id=student_id,
            subject=subject,
            conversation_id=conversation_id,
            message_id=message_id,
            knowledge_point=self.infer_knowledge_point(evidence),
            error_type=error_type,
            evidence_text=evidence[:1000],
            confidence=0.82,
        )
        db.add(event)
        db.flush()
        self._rebuild_profile(db, student_id=student_id, subject=subject)
        return event

    def classify_error_type(self, evidence: str) -> str:
        text = evidence.strip()
        if any(keyword in text for keyword in ("受力图", "电路图", "图景", "画图", "没画", "漏画", "漏掉", "看不见")):
            return "diagram_establishment"
        if any(keyword in text for keyword in ("公式", "适用条件", "套用", "欧姆定律", "F=ma", "P=UI")):
            return "formula_misuse"
        if any(keyword in text for keyword in ("单位", "换算", "计算", "比例", "方程", "代数")):
            return "math_tool"
        if any(keyword in text for keyword in ("概念", "混淆", "不理解", "压强", "压力", "电压", "电流", "惯性")):
            return "concept_confusion"
        if any(keyword in text for keyword in ("过程", "阶段", "状态", "分段", "开关变化", "初态", "末态")):
            return "process_analysis"
        return "process_analysis"

    def infer_knowledge_point(self, evidence: str) -> str:
        text = evidence.strip()
        if any(keyword in text for keyword in ("受力", "摩擦", "支持力", "斜面")):
            return "受力分析"
        if any(keyword in text for keyword in ("电路", "电流", "电压", "电阻", "欧姆")):
            return "电路分析"
        if any(keyword in text for keyword in ("能量", "动能", "势能", "机械能")):
            return "能量守恒"
        if any(keyword in text for keyword in ("单位", "换算")):
            return "单位换算"
        if any(keyword in text for keyword in ("公式", "适用条件")):
            return "公式适用条件"
        return "物理过程分析"

    def profile_summary(self, db: Session, *, student_id: int, subject: str = "物理") -> dict[str, Any]:
        profile = db.scalar(
            select(StudentSkillProfile).where(
                StudentSkillProfile.student_id == student_id,
                StudentSkillProfile.subject == subject,
            )
        )
        if not profile:
            return self.empty_summary(subject)
        return self._normalize_summary(subject, profile.profile_json or {})

    def empty_summary(self, subject: str = "物理") -> dict[str, Any]:
        return {
            "subject": subject,
            "total_events": 0,
            "top_error_type": None,
            "top_error_label": None,
            "error_counts": {},
            "recent_weaknesses": [],
        }

    def _rebuild_profile(self, db: Session, *, student_id: int, subject: str) -> None:
        events = db.scalars(
            select(StudentErrorEvent)
            .where(StudentErrorEvent.student_id == student_id, StudentErrorEvent.subject == subject)
            .order_by(StudentErrorEvent.created_at.desc(), StudentErrorEvent.id.desc())
        ).all()
        counts = Counter(event.error_type for event in events)
        top_error_type = counts.most_common(1)[0][0] if counts else None
        recent_weaknesses = []
        for event in events[:5]:
            if event.knowledge_point and event.knowledge_point not in recent_weaknesses:
                recent_weaknesses.append(event.knowledge_point)
        profile_json = {
            "subject": subject,
            "total_events": len(events),
            "top_error_type": top_error_type,
            "top_error_label": ERROR_TYPE_LABELS.get(top_error_type or "") if top_error_type else None,
            "error_counts": dict(counts),
            "recent_weaknesses": recent_weaknesses,
        }
        profile = db.scalar(
            select(StudentSkillProfile).where(
                StudentSkillProfile.student_id == student_id,
                StudentSkillProfile.subject == subject,
            )
        )
        if profile:
            profile.profile_json = profile_json
        else:
            profile = StudentSkillProfile(student_id=student_id, subject=subject, profile_json=profile_json)
        db.add(profile)
        db.flush()

    def _normalize_summary(self, subject: str, raw: dict[str, Any]) -> dict[str, Any]:
        summary = self.empty_summary(subject)
        summary.update({key: raw.get(key) for key in summary if key in raw})
        summary["subject"] = str(summary.get("subject") or subject)
        summary["total_events"] = int(summary.get("total_events") or 0)
        top_error_type = summary.get("top_error_type")
        if top_error_type and not summary.get("top_error_label"):
            summary["top_error_label"] = ERROR_TYPE_LABELS.get(str(top_error_type))
        if not isinstance(summary.get("error_counts"), dict):
            summary["error_counts"] = {}
        if not isinstance(summary.get("recent_weaknesses"), list):
            summary["recent_weaknesses"] = []
        return summary


physics_error_profile_service = PhysicsErrorProfileService()
