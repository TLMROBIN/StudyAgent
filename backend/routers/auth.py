from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.dependencies import CurrentUser, DbSession, oauth2_scheme
from backend.models.schemas import (
    LogoutRequest,
    PasswordChangeRequest,
    RefreshTokenRequest,
    StaffLoginRequest,
    StudentLoginRequest,
    TokenResponse,
    UserRead,
)
from backend.models.user import UserRole
from backend.security import decode_token, verify_password
from backend.services.auth_service import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/student/login", response_model=TokenResponse)
def student_login(payload: StudentLoginRequest, request: Request, db: DbSession) -> TokenResponse:
    user = auth_service.authenticate_student(db, payload.student_no, payload.password, _client_ip(request))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    tokens = auth_service.issue_token_pair(user)
    return TokenResponse(**tokens)


@router.post("/staff/login", response_model=TokenResponse)
def staff_login(payload: StaffLoginRequest, request: Request, db: DbSession) -> TokenResponse:
    user = auth_service.authenticate_staff(db, payload.username, payload.password, _client_ip(request))
    if not user or user.role == UserRole.STUDENT:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    tokens = auth_service.issue_token_pair(user)
    return TokenResponse(**tokens)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(payload: RefreshTokenRequest, db: DbSession) -> TokenResponse:
    try:
        decoded = decode_token(payload.refresh_token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc

    user, tokens = auth_service.rotate_refresh_token(db, decoded)
    if not user or not tokens:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")
    return TokenResponse(**tokens)


@router.post("/logout")
def logout(
    payload: LogoutRequest,
    db: DbSession,
    current_user: CurrentUser,
    access_token: Annotated[str, Depends(oauth2_scheme)],
) -> dict[str, str]:
    try:
        refresh_payload = decode_token(payload.refresh_token)
        access_payload = decode_token(access_token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    family_id = refresh_payload.get("family_id")
    if family_id:
        auth_service.revoke_family(current_user.id, family_id)

    access_jti = access_payload.get("jti")
    access_exp = int(access_payload.get("exp", 0))
    ttl_seconds = max(access_exp - int(datetime.now(UTC).timestamp()), 1)
    if access_jti:
        auth_service.revoke_access_token(access_jti, ttl_seconds=ttl_seconds)

    return {"status": "ok"}


@router.get("/me", response_model=UserRead)
def read_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@router.post("/change-password", response_model=UserRead)
def change_password(payload: PasswordChangeRequest, db: DbSession, current_user: CurrentUser) -> UserRead:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password incorrect")
    updated = auth_service.update_password(db, current_user, payload.new_password)
    return UserRead.model_validate(updated)
