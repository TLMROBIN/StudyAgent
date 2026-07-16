from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

import json
import logging
from pathlib import Path

from backend.dependencies import CurrentAdmin, CurrentUser, DbSession
from backend.models.agent_config import AgentConfig
from backend.models.schemas import AgentConfigCreate, AgentConfigRead
from backend.services.audit_service import audit_service

router = APIRouter(prefix="/api/agent-config", tags=["agent-config"])

logger = logging.getLogger(__name__)

SUBJECT_PROMPT_DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "subject_prompt_defaults.json"


@router.get("/subject-prompt-defaults")
def get_subject_prompt_defaults(current_user: CurrentAdmin) -> dict[str, str]:
    """九科 subject_prompts 推荐默认模板（只读）。文件缺失或损坏时返回空 dict。"""
    try:
        raw = json.loads(SUBJECT_PROMPT_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("subject_prompt_defaults.json 不可用: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}


@router.get("/", response_model=list[AgentConfigRead])
def list_agent_configs(db: DbSession, current_user: CurrentUser) -> list[AgentConfigRead]:
    items = db.scalars(select(AgentConfig).order_by(AgentConfig.version.desc())).all()
    return [AgentConfigRead.model_validate(item) for item in items]


@router.get("/active", response_model=AgentConfigRead)
def get_active_agent_config(db: DbSession, current_user: CurrentUser) -> AgentConfigRead:
    item = db.scalar(select(AgentConfig).where(AgentConfig.is_active.is_(True)).order_by(AgentConfig.version.desc()))
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active config not found")
    return AgentConfigRead.model_validate(item)


@router.post("/", response_model=AgentConfigRead, status_code=status.HTTP_201_CREATED)
def create_agent_config(payload: AgentConfigCreate, db: DbSession, current_user: CurrentAdmin, request: Request) -> AgentConfigRead:
    max_version = db.scalar(select(func.max(AgentConfig.version))) or 0
    item = AgentConfig(
        version=max_version + 1,
        system_prompt=payload.system_prompt,
        guidance_params=payload.guidance_params,
        subject_prompts=payload.subject_prompts,
        filter_rules=payload.filter_rules,
        is_active=False,
        created_by=current_user.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="create_agent_config",
        target_type="agent_config",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"version": item.version},
    )
    return AgentConfigRead.model_validate(item)


@router.post("/{config_id}/activate", response_model=AgentConfigRead)
def activate_agent_config(config_id: int, db: DbSession, current_user: CurrentAdmin, request: Request) -> AgentConfigRead:
    item = db.get(AgentConfig, config_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config not found")

    current_active = db.scalars(select(AgentConfig).where(AgentConfig.is_active.is_(True))).all()
    for active in current_active:
        active.is_active = False
        db.add(active)

    item.is_active = True
    db.add(item)
    db.commit()
    db.refresh(item)
    from backend.routers.chat import invalidate_active_agent_config_cache

    invalidate_active_agent_config_cache()
    audit_service.log(
        db,
        actor=current_user,
        action="activate_agent_config",
        target_type="agent_config",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"version": item.version},
    )
    return AgentConfigRead.model_validate(item)


@router.get("/compare")
def compare_agent_configs(left: int, right: int, db: DbSession, current_user: CurrentAdmin) -> dict:
    left_item = db.get(AgentConfig, left)
    right_item = db.get(AgentConfig, right)
    if not left_item or not right_item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config not found")

    return {
        "left": left_item.version,
        "right": right_item.version,
        "diff": {
            "system_prompt_changed": left_item.system_prompt != right_item.system_prompt,
            "guidance_params_changed": left_item.guidance_params != right_item.guidance_params,
            "subject_prompts_changed": left_item.subject_prompts != right_item.subject_prompts,
            "filter_rules_changed": left_item.filter_rules != right_item.filter_rules,
        },
    }
