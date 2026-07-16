from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from backend.dependencies import CurrentAdmin, CurrentStudent, DbSession
from backend.models.agent_role import AgentRole, AgentRoleRevision
from backend.models.schemas import (
    AgentRoleCreate,
    AgentRoleEnabledUpdate,
    AgentRoleImportResult,
    AgentRolePublicRead,
    AgentRoleRead,
    AgentRoleRevisionRead,
    AgentRoleUpdate,
)
from backend.services.agent_role_service import agent_role_service
from backend.services.audit_service import audit_service
from backend.subjects import is_valid_subject


router = APIRouter(prefix="/api/agent-roles", tags=["agent-roles"])
logger = logging.getLogger(__name__)
ROLE_DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "agent_role_defaults.json"


def _revision_read(revision: AgentRoleRevision) -> AgentRoleRevisionRead:
    return AgentRoleRevisionRead(
        id=revision.id,
        revision=revision.revision,
        style_config=revision.style_config,
        renderer_version=revision.renderer_version,
        content_hash=revision.content_hash,
        created_at=revision.created_at,
    )


def _role_read(role: AgentRole, revision: AgentRoleRevision) -> AgentRoleRead:
    return AgentRoleRead(
        id=role.id,
        name=role.name,
        display_name=role.display_name,
        emoji=role.emoji,
        description=role.description,
        subjects=role.subjects,
        current_revision_id=revision.id,
        is_enabled=role.is_enabled,
        sort_order=role.sort_order,
        current_revision=_revision_read(revision),
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def _public_read(role: AgentRole) -> AgentRolePublicRead:
    return AgentRolePublicRead(
        id=role.id,
        name=role.name,
        display_name=role.display_name,
        emoji=role.emoji,
        description=role.description,
        subjects=role.subjects,
    )


def _read_defaults() -> list[AgentRoleCreate]:
    try:
        raw = json.loads(ROLE_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("agent_role_defaults.json unavailable: %s", exc)
        return []
    if not isinstance(raw, list):
        return []
    defaults: list[AgentRoleCreate] = []
    for item in raw:
        try:
            defaults.append(AgentRoleCreate.model_validate(item))
        except ValueError as exc:
            logger.warning("invalid default agent role ignored: %s", exc)
    return defaults


@router.get("/role-defaults", response_model=list[AgentRoleCreate])
def get_role_defaults(current_user: CurrentAdmin) -> list[AgentRoleCreate]:
    return _read_defaults()


@router.post("/import-defaults", response_model=AgentRoleImportResult)
def import_role_defaults(db: DbSession, current_user: CurrentAdmin, request: Request) -> AgentRoleImportResult:
    created = 0
    skipped = 0
    for payload in _read_defaults():
        if db.scalar(select(AgentRole.id).where(AgentRole.name == payload.name)) is not None:
            skipped += 1
            continue
        agent_role_service.create_role(db, payload, created_by=current_user.id)
        created += 1
    db.commit()
    audit_service.log(
        db,
        actor=current_user,
        action="import_agent_role_defaults",
        target_type="agent_role",
        target_id=None,
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"created": created, "skipped": skipped},
    )
    return AgentRoleImportResult(created=created, skipped=skipped)


@router.get("/enabled", response_model=list[AgentRolePublicRead])
def list_enabled_roles(subject: str, db: DbSession, current_user: CurrentStudent) -> list[AgentRolePublicRead]:
    if not is_valid_subject(subject):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported subject")
    return [_public_read(role) for role in agent_role_service.list_enabled(db, subject)]


@router.get("/", response_model=list[AgentRoleRead])
def list_roles(db: DbSession, current_user: CurrentAdmin) -> list[AgentRoleRead]:
    return [_role_read(role, revision) for role, revision in agent_role_service.list_roles(db)]


@router.post("/", response_model=AgentRoleRead, status_code=status.HTTP_201_CREATED)
def create_role(payload: AgentRoleCreate, db: DbSession, current_user: CurrentAdmin, request: Request) -> AgentRoleRead:
    try:
        role, revision = agent_role_service.create_role(db, payload, created_by=current_user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.refresh(role)
    db.refresh(revision)
    audit_service.log(
        db,
        actor=current_user,
        action="create_agent_role",
        target_type="agent_role",
        target_id=str(role.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"name": role.name, "revision": revision.revision, "content_hash": revision.content_hash},
    )
    return _role_read(role, revision)


@router.put("/{role_id}", response_model=AgentRoleRead)
def update_role(
    role_id: int,
    payload: AgentRoleUpdate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> AgentRoleRead:
    role = db.get(AgentRole, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    before_revision_id = role.current_revision_id
    role, revision = agent_role_service.update_role(db, role, payload, created_by=current_user.id)
    db.commit()
    db.refresh(role)
    db.refresh(revision)
    audit_service.log(
        db,
        actor=current_user,
        action="update_agent_role",
        target_type="agent_role",
        target_id=str(role.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={
            "name": role.name,
            "previous_revision_id": before_revision_id,
            "revision": revision.revision,
            "content_hash": revision.content_hash,
        },
    )
    return _role_read(role, revision)


@router.put("/{role_id}/enabled", response_model=AgentRoleRead)
def set_role_enabled(
    role_id: int,
    payload: AgentRoleEnabledUpdate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> AgentRoleRead:
    role = db.get(AgentRole, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    revision = agent_role_service.get_revision(db, role.current_revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role has no active revision")
    role.is_enabled = payload.is_enabled
    db.add(role)
    db.commit()
    db.refresh(role)
    audit_service.log(
        db,
        actor=current_user,
        action="enable_agent_role" if payload.is_enabled else "disable_agent_role",
        target_type="agent_role",
        target_id=str(role.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"name": role.name, "is_enabled": role.is_enabled},
    )
    return _role_read(role, revision)


@router.get("/{role_id}/revisions", response_model=list[AgentRoleRevisionRead])
def list_role_revisions(role_id: int, db: DbSession, current_user: CurrentAdmin) -> list[AgentRoleRevisionRead]:
    if db.get(AgentRole, role_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return [_revision_read(revision) for revision in agent_role_service.list_revisions(db, role_id)]
