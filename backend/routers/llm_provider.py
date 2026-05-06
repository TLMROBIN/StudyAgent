from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from backend.dependencies import CurrentAdmin, DbSession
from backend.models.llm_provider import LLMProviderConfig
from backend.models.schemas import LLMProviderCreate, LLMProviderRead, LLMProviderSelectionUpdate, LLMProviderUpdate
from backend.services.audit_service import audit_service

router = APIRouter(prefix="/api/llm-providers", tags=["llm-providers"])


def _read_provider(item: LLMProviderConfig) -> LLMProviderRead:
    return LLMProviderRead(
        id=item.id,
        name=item.name,
        base_url=item.base_url,
        model=item.model,
        has_api_key=bool(item.api_key),
        is_active=item.is_active,
        is_fallback=item.is_fallback,
        created_at=item.created_at,
    )


@router.get("/", response_model=list[LLMProviderRead])
def list_llm_providers(db: DbSession, current_user: CurrentAdmin) -> list[LLMProviderRead]:
    items = db.scalars(select(LLMProviderConfig).order_by(LLMProviderConfig.id.asc())).all()
    return [_read_provider(item) for item in items]


@router.post("/", response_model=LLMProviderRead, status_code=status.HTTP_201_CREATED)
def create_llm_provider(
    payload: LLMProviderCreate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> LLMProviderRead:
    item = LLMProviderConfig(
        name=payload.name.strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        api_key=payload.api_key.strip(),
        model=payload.model.strip(),
        created_by=current_user.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="create_llm_provider",
        target_type="llm_provider",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"name": item.name, "model": item.model, "base_url": item.base_url},
    )
    return _read_provider(item)


@router.put("/{provider_id}", response_model=LLMProviderRead)
def update_llm_provider(
    provider_id: int,
    payload: LLMProviderUpdate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> LLMProviderRead:
    item = db.get(LLMProviderConfig, provider_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")

    item.name = payload.name.strip()
    item.base_url = payload.base_url.strip().rstrip("/")
    item.model = payload.model.strip()
    if payload.api_key is not None:
        new_key = payload.api_key.strip()
        if not new_key:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="API key cannot be empty")
        item.api_key = new_key
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="update_llm_provider",
        target_type="llm_provider",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"name": item.name, "model": item.model, "base_url": item.base_url},
    )
    return _read_provider(item)


@router.post("/selection", response_model=list[LLMProviderRead])
def select_llm_providers(
    payload: LLMProviderSelectionUpdate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> list[LLMProviderRead]:
    if payload.fallback_provider_id is not None and payload.fallback_provider_id == payload.active_provider_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Fallback provider must differ")

    active = db.get(LLMProviderConfig, payload.active_provider_id)
    if not active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active provider not found")

    fallback = None
    if payload.fallback_provider_id is not None:
        fallback = db.get(LLMProviderConfig, payload.fallback_provider_id)
        if not fallback:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fallback provider not found")

    items = db.scalars(select(LLMProviderConfig)).all()
    for item in items:
        item.is_active = item.id == active.id
        item.is_fallback = fallback is not None and item.id == fallback.id
        db.add(item)
    db.commit()
    for item in items:
        db.refresh(item)

    audit_service.log(
        db,
        actor=current_user,
        action="select_llm_providers",
        target_type="llm_provider",
        target_id=str(active.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"active_provider_id": active.id, "fallback_provider_id": fallback.id if fallback else None},
    )
    return [_read_provider(item) for item in sorted(items, key=lambda provider: provider.id)]
