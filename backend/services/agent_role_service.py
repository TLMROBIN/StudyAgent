from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.agent_role import AgentRole, AgentRoleRevision
from backend.models.schemas import AgentRoleCreate, AgentRoleStyleConfig, AgentRoleUpdate


ROLE_RENDERER_VERSION = "v1"
ROLE_SAFETY_RIDER = (
    "本段只定义表达风格，不得新增任务、改变事实、覆盖前述系统约束或改变教学阶段；"
    "不得直接给出具体习题的最终答案、完整解题步骤或可直接抄写的标准解。"
)
ROLE_PROMPT_MAX_CHARS = 600

_TONE_TEXT = {
    "warm": "语气亲切、耐心，给学生安全感。",
    "rigorous": "语气严谨、克制，概念和因果表述准确。",
    "humorous": "语气轻松并带少量温和幽默，但不玩梗、不分散注意力。",
    "calm": "语气沉稳、平和，让学生有时间逐步思考。",
    "poetic": "措辞富有画面感，但必须保持清楚、简洁且不牺牲学科准确性。",
}
_PACE_TEXT = {
    "concise_steps": "表达节奏采用短小步骤，每次只推进一个思考点。",
    "guided_questions": "表达节奏以循序追问为主，每次只提出同一思考点上的问题。",
    "intuition_then_concept": "先建立直觉或具体感受，再引向概念和规律。",
    "example_then_summary": "先用一个具体例子启发，再请学生自己概括规律。",
}
_ANALOGY_TEXT = {
    "daily_life": "优先使用贴近日常生活的简短类比。",
    "experiment": "优先使用可观察、可想象的实验情境。",
    "thought_experiment": "可使用简短思想实验帮助学生检验直觉。",
    "literary_imagery": "可使用克制的文学意象帮助理解语言或情境。",
    "historical_context": "可用简短历史情境帮助理解背景与因果。",
    "minimal": "少用类比，优先用清楚的事实和问题推进。",
}
_FORMALITY_TEXT = {
    "conversational": "措辞口语化、自然，不使用网络流行语。",
    "natural": "措辞自然清楚，兼顾亲和与规范。",
    "formal": "措辞规范、正式，但避免生硬和长篇说教。",
}
_SENTENCE_TEXT = {
    "short": "以短句为主。",
    "medium": "句子长度适中。",
    "varied": "长短句结合，重点处使用短句。",
}
_TRAIT_TEXT = {
    "simple_analogies": "类比必须简单且明确指出对应关系。",
    "student_restate": "适时请学生用自己的话复述当前理解。",
    "evidence_first": "先追问证据或已知条件，再讨论结论。",
    "thought_experiments": "适时用思想实验检查学生的直觉。",
    "literary_imagery": "适时用简洁意象帮助学生形成画面。",
    "concise_language": "删除不必要的铺垫和重复表达。",
    "gentle_humor": "可加入一句温和幽默，但不能嘲讽学生。",
}


@dataclass(frozen=True)
class ResolvedRoleSnapshot:
    requested_role_id: int | None
    applied: bool
    status: str
    role_id: int | None = None
    role_name: str | None = None
    display_name: str | None = None
    revision_id: int | None = None
    revision: int | None = None
    content_hash: str | None = None
    rendered_prompt: str | None = None

    @classmethod
    def none(cls, requested_role_id: int | None = None, *, status: str = "none") -> "ResolvedRoleSnapshot":
        return cls(requested_role_id=requested_role_id, applied=False, status=status)

    def public_snapshot(self) -> dict[str, Any] | None:
        if not self.role_id or not self.revision_id:
            return None
        return {
            "role_id": self.role_id,
            "name": self.role_name,
            "display_name": self.display_name,
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }

    def replay_snapshot(self) -> dict[str, Any]:
        return {
            "requested_role_id": self.requested_role_id,
            "applied": self.applied,
            "status": self.status,
            "role_id": self.role_id,
            "role_name": self.role_name,
            "display_name": self.display_name,
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "rendered_prompt": self.rendered_prompt,
        }

    def meta_payload(self) -> dict[str, Any]:
        return {
            "role_status": self.status,
            "role_applied": self.applied,
            "role_id": self.role_id,
            "role_name": self.role_name,
            "role_display_name": self.display_name,
            "role_revision": self.revision,
        }

    def bypassed(self) -> "ResolvedRoleSnapshot":
        return replace(self, applied=False, status="bypassed", rendered_prompt=None)


def snapshot_from_replay(raw: dict[str, Any] | None, requested_role_id: int | None) -> ResolvedRoleSnapshot | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ResolvedRoleSnapshot(
            requested_role_id=raw.get("requested_role_id", requested_role_id),
            applied=bool(raw.get("applied")),
            status=str(raw.get("status") or "none"),
            role_id=int(raw["role_id"]) if raw.get("role_id") is not None else None,
            role_name=str(raw["role_name"]) if raw.get("role_name") is not None else None,
            display_name=str(raw["display_name"]) if raw.get("display_name") is not None else None,
            revision_id=int(raw["revision_id"]) if raw.get("revision_id") is not None else None,
            revision=int(raw["revision"]) if raw.get("revision") is not None else None,
            content_hash=str(raw["content_hash"]) if raw.get("content_hash") is not None else None,
            rendered_prompt=str(raw["rendered_prompt"]) if raw.get("rendered_prompt") is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def render_style_prompt(style_config: AgentRoleStyleConfig | dict[str, Any]) -> tuple[str, str]:
    config = style_config if isinstance(style_config, AgentRoleStyleConfig) else AgentRoleStyleConfig.model_validate(style_config)
    sections = [
        "<role_style_data>",
        _TONE_TEXT[config.tone],
        _PACE_TEXT[config.explanation_pace],
        _ANALOGY_TEXT[config.analogy_style],
        _FORMALITY_TEXT[config.formality],
        _SENTENCE_TEXT[config.sentence_length],
    ]
    sections.extend(_TRAIT_TEXT[trait] for trait in config.traits)
    sections.extend([ROLE_SAFETY_RIDER, "</role_style_data>"])
    rendered = "\n".join(sections)
    if len(rendered) > ROLE_PROMPT_MAX_CHARS:
        raise ValueError("rendered role prompt exceeds length limit")
    canonical = json.dumps(config.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = sha256(f"{ROLE_RENDERER_VERSION}\n{canonical}\n{rendered}".encode("utf-8")).hexdigest()
    return rendered, digest


class AgentRoleService:
    def create_role(self, db: Session, payload: AgentRoleCreate, *, created_by: int | None) -> tuple[AgentRole, AgentRoleRevision]:
        existing = db.scalar(select(AgentRole).where(AgentRole.name == payload.name))
        if existing:
            raise ValueError("role name already exists")
        role = AgentRole(
            name=payload.name,
            display_name=payload.display_name,
            emoji=payload.emoji or None,
            description=payload.description,
            subjects=payload.subjects,
            is_enabled=False,
            sort_order=payload.sort_order,
            created_by=created_by,
        )
        db.add(role)
        db.flush()
        revision = self._create_revision(db, role, payload.style_config, created_by=created_by)
        role.current_revision_id = revision.id
        db.add(role)
        db.flush()
        return role, revision

    def update_role(self, db: Session, role: AgentRole, payload: AgentRoleUpdate, *, created_by: int | None) -> tuple[AgentRole, AgentRoleRevision]:
        role.display_name = payload.display_name
        role.emoji = payload.emoji or None
        role.description = payload.description
        role.subjects = payload.subjects
        role.sort_order = payload.sort_order
        current = self.get_revision(db, role.current_revision_id)
        next_config = payload.style_config.model_dump(mode="json")
        if current is None or current.style_config != next_config:
            current = self._create_revision(db, role, payload.style_config, created_by=created_by)
            role.current_revision_id = current.id
        db.add(role)
        db.flush()
        return role, current

    def resolve(self, db: Session, role_id: int | None, subject: str) -> ResolvedRoleSnapshot:
        if role_id is None:
            return ResolvedRoleSnapshot.none()
        row = db.execute(
            select(AgentRole, AgentRoleRevision)
            .outerjoin(AgentRoleRevision, AgentRoleRevision.id == AgentRole.current_revision_id)
            .where(AgentRole.id == role_id)
        ).one_or_none()
        if row is None:
            return ResolvedRoleSnapshot.none(role_id, status="not_found")
        role, revision = row
        if not role.is_enabled:
            return ResolvedRoleSnapshot.none(role_id, status="disabled")
        if role.subjects and subject not in role.subjects:
            return ResolvedRoleSnapshot.none(role_id, status="subject_mismatch")
        if revision is None:
            return ResolvedRoleSnapshot.none(role_id, status="misconfigured")
        return ResolvedRoleSnapshot(
            requested_role_id=role_id,
            applied=True,
            status="applied",
            role_id=role.id,
            role_name=role.name,
            display_name=role.display_name,
            revision_id=revision.id,
            revision=revision.revision,
            content_hash=revision.content_hash,
            rendered_prompt=revision.rendered_prompt,
        )

    @staticmethod
    def get_revision(db: Session, revision_id: int | None) -> AgentRoleRevision | None:
        return db.get(AgentRoleRevision, revision_id) if revision_id else None

    @staticmethod
    def list_roles(db: Session) -> list[tuple[AgentRole, AgentRoleRevision]]:
        rows = db.execute(
            select(AgentRole, AgentRoleRevision)
            .join(AgentRoleRevision, AgentRoleRevision.id == AgentRole.current_revision_id)
            .order_by(AgentRole.sort_order.asc(), AgentRole.id.asc())
        ).all()
        return [(role, revision) for role, revision in rows]

    @staticmethod
    def list_enabled(db: Session, subject: str) -> list[AgentRole]:
        roles = db.scalars(
            select(AgentRole)
            .where(AgentRole.is_enabled.is_(True))
            .order_by(AgentRole.sort_order.asc(), AgentRole.id.asc())
        ).all()
        return [role for role in roles if not role.subjects or subject in role.subjects]

    @staticmethod
    def list_revisions(db: Session, role_id: int) -> list[AgentRoleRevision]:
        return list(
            db.scalars(
                select(AgentRoleRevision)
                .where(AgentRoleRevision.role_id == role_id)
                .order_by(AgentRoleRevision.revision.desc())
            ).all()
        )

    def _create_revision(
        self,
        db: Session,
        role: AgentRole,
        style_config: AgentRoleStyleConfig,
        *,
        created_by: int | None,
    ) -> AgentRoleRevision:
        rendered_prompt, content_hash = render_style_prompt(style_config)
        latest = db.scalar(select(func.max(AgentRoleRevision.revision)).where(AgentRoleRevision.role_id == role.id)) or 0
        revision = AgentRoleRevision(
            role_id=role.id,
            revision=latest + 1,
            style_config=style_config.model_dump(mode="json"),
            renderer_version=ROLE_RENDERER_VERSION,
            rendered_prompt=rendered_prompt,
            content_hash=content_hash,
            created_by=created_by,
        )
        db.add(revision)
        db.flush()
        return revision


agent_role_service = AgentRoleService()
