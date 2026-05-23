from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.dependencies import CurrentAdmin, DbSession
from backend.models.llm_account import AccountBillingType, LLMProviderAccount
from backend.models.llm_model import LLMModelConfig, LLMQuotaPolicy, QuotaBillingMode
from backend.models.llm_provider import LLMProviderConfig
from backend.models.schemas import (
    LLMModelConfigCreate,
    LLMModelConfigRead,
    LLMModelConfigUpdate,
    LLMProviderAccountCreate,
    LLMProviderAccountRead,
    LLMProviderAccountUpdate,
    LLMProviderCreate,
    LLMProviderRead,
    LLMProviderSelectionUpdate,
    LLMProviderUpdate,
)
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


def _read_account(item: LLMProviderAccount) -> LLMProviderAccountRead:
    return LLMProviderAccountRead(
        id=item.id,
        provider_name=item.provider_name,
        display_name=item.display_name,
        base_url=item.base_url,
        account_billing_type=item.account_billing_type.value,
        is_enabled=item.is_enabled,
        has_api_key=bool(item.api_key),
        created_at=item.created_at,
    )


def _apply_policy(policy: LLMQuotaPolicy, payload) -> None:
    policy.billing_mode = QuotaBillingMode(payload.billing_mode)
    policy.user_daily_request_limit = payload.user_daily_request_limit
    policy.user_daily_token_limit = payload.user_daily_token_limit
    policy.school_daily_request_limit = payload.school_daily_request_limit
    policy.school_daily_token_limit = payload.school_daily_token_limit
    policy.provider_rolling_5h_request_limit = payload.provider_rolling_5h_request_limit
    policy.provider_weekly_request_limit = payload.provider_weekly_request_limit
    policy.max_completion_tokens = payload.max_completion_tokens
    policy.count_cache_hit = payload.count_cache_hit
    policy.fail_closed_on_store_error = payload.fail_closed_on_store_error


def _read_model(item: LLMModelConfig) -> LLMModelConfigRead:
    return LLMModelConfigRead.model_validate(item)


@router.get("/", response_model=list[LLMProviderRead])
def list_llm_providers(db: DbSession, current_user: CurrentAdmin) -> list[LLMProviderRead]:
    items = db.scalars(select(LLMProviderConfig).order_by(LLMProviderConfig.id.asc())).all()
    return [_read_provider(item) for item in items]


@router.get("/accounts", response_model=list[LLMProviderAccountRead])
def list_provider_accounts(db: DbSession, current_user: CurrentAdmin) -> list[LLMProviderAccountRead]:
    items = db.scalars(select(LLMProviderAccount).order_by(LLMProviderAccount.id.asc())).all()
    return [_read_account(item) for item in items]


@router.post("/accounts", response_model=LLMProviderAccountRead, status_code=status.HTTP_201_CREATED)
def create_provider_account(
    payload: LLMProviderAccountCreate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> LLMProviderAccountRead:
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="API key cannot be empty")
    item = LLMProviderAccount(
        provider_name=payload.provider_name.strip(),
        display_name=payload.display_name.strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        api_key=api_key,
        account_billing_type=AccountBillingType(payload.account_billing_type),
        is_enabled=payload.is_enabled,
        created_by=current_user.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="create_llm_provider_account",
        target_type="llm_provider_account",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"provider_name": item.provider_name, "base_url": item.base_url},
    )
    return _read_account(item)


@router.put("/accounts/{account_id}", response_model=LLMProviderAccountRead)
def update_provider_account(
    account_id: int,
    payload: LLMProviderAccountUpdate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> LLMProviderAccountRead:
    item = db.get(LLMProviderAccount, account_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider account not found")
    item.provider_name = payload.provider_name.strip()
    item.display_name = payload.display_name.strip()
    item.base_url = payload.base_url.strip().rstrip("/")
    item.account_billing_type = AccountBillingType(payload.account_billing_type)
    item.is_enabled = payload.is_enabled
    if payload.api_key is not None and payload.api_key.strip():
        item.api_key = payload.api_key.strip()
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="update_llm_provider_account",
        target_type="llm_provider_account",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"provider_name": item.provider_name, "base_url": item.base_url},
    )
    return _read_account(item)


@router.get("/models", response_model=list[LLMModelConfigRead])
def list_model_configs(db: DbSession, current_user: CurrentAdmin) -> list[LLMModelConfigRead]:
    items = db.scalars(
        select(LLMModelConfig)
        .options(selectinload(LLMModelConfig.quota_policy))
        .order_by(LLMModelConfig.sort_order.asc(), LLMModelConfig.id.asc())
    ).all()
    return [_read_model(item) for item in items]


@router.post("/models", response_model=LLMModelConfigRead, status_code=status.HTTP_201_CREATED)
def create_model_config(
    payload: LLMModelConfigCreate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> LLMModelConfigRead:
    account = db.get(LLMProviderAccount, payload.provider_account_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider account not found")
    existing = db.scalar(select(LLMModelConfig).where(LLMModelConfig.model_key == payload.model_key))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Model key already exists")
    item = LLMModelConfig(
        model_key=payload.model_key,
        display_name=payload.display_name.strip(),
        description=payload.description.strip(),
        provider_account_id=payload.provider_account_id,
        provider_model=payload.provider_model.strip(),
        capability_text=payload.capability_text,
        capability_vision=payload.capability_vision,
        is_enabled=payload.is_enabled,
        is_primary=payload.is_primary,
        is_fallback=payload.is_fallback,
        sort_order=payload.sort_order,
        created_by=current_user.id,
    )
    policy = LLMQuotaPolicy(model_config=item)
    _apply_policy(policy, payload.quota_policy)
    db.add(item)
    db.add(policy)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="create_llm_model_config",
        target_type="llm_model_config",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"model_key": item.model_key, "billing_mode": policy.billing_mode.value},
    )
    return _read_model(item)


@router.put("/models/{model_id}", response_model=LLMModelConfigRead)
def update_model_config(
    model_id: int,
    payload: LLMModelConfigUpdate,
    db: DbSession,
    current_user: CurrentAdmin,
    request: Request,
) -> LLMModelConfigRead:
    item = db.scalar(
        select(LLMModelConfig).options(selectinload(LLMModelConfig.quota_policy)).where(LLMModelConfig.id == model_id)
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model config not found")
    duplicate = db.scalar(
        select(LLMModelConfig).where(LLMModelConfig.model_key == payload.model_key, LLMModelConfig.id != model_id)
    )
    if duplicate:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Model key already exists")
    if not db.get(LLMProviderAccount, payload.provider_account_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider account not found")

    old_detail = {
        "model_key": item.model_key,
        "enabled": item.is_enabled,
        "billing_mode": item.quota_policy.billing_mode.value if item.quota_policy else None,
    }
    item.model_key = payload.model_key
    item.display_name = payload.display_name.strip()
    item.description = payload.description.strip()
    item.provider_account_id = payload.provider_account_id
    item.provider_model = payload.provider_model.strip()
    item.capability_text = payload.capability_text
    item.capability_vision = payload.capability_vision
    item.is_enabled = payload.is_enabled
    item.is_primary = payload.is_primary
    item.is_fallback = payload.is_fallback
    item.sort_order = payload.sort_order
    policy = item.quota_policy or LLMQuotaPolicy(model_config=item)
    _apply_policy(policy, payload.quota_policy)
    db.add(item)
    db.add(policy)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="update_llm_model_config",
        target_type="llm_model_config",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"old": old_detail, "new": {"model_key": item.model_key, "billing_mode": policy.billing_mode.value}},
    )
    return _read_model(item)


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
