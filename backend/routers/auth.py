from datetime import UTC, datetime
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

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
from backend.security import decode_token, verify_password
from backend.services.auth_service import auth_service
from backend.services.oidc_service import OidcAuthError, oidc_service
from backend.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
OIDC_DISABLED_MESSAGE = "统一平台登录暂未启用，请使用账号密码登录。"
PASSWORD_LOGIN_DISABLED_MESSAGE = "本系统已切换为统一认证登录，请通过统一平台入口登录"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _ensure_oidc_enabled() -> None:
    if not get_settings().oidc_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=OIDC_DISABLED_MESSAGE)


@router.post("/student/login")
def student_login(payload: StudentLoginRequest, request: Request, db: DbSession) -> None:
    # 本地账号密码登录已停用，仅保留统一认证（OIDC）登录入口
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PASSWORD_LOGIN_DISABLED_MESSAGE)


@router.post("/staff/login")
def staff_login(payload: StaffLoginRequest, request: Request, db: DbSession) -> None:
    # 本地账号密码登录已停用，仅保留统一认证（OIDC）登录入口
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PASSWORD_LOGIN_DISABLED_MESSAGE)


@router.get("/oidc/config")
def oidc_config() -> dict[str, bool | str]:
    return {
        "enabled": get_settings().oidc_enabled,
        "message": "" if get_settings().oidc_enabled else OIDC_DISABLED_MESSAGE,
    }


@router.get("/oidc/login")
def oidc_login() -> RedirectResponse:
    _ensure_oidc_enabled()
    login_state = oidc_service.create_login_state()
    response = RedirectResponse(login_state["authorization_url"], status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        "studyagent_oidc_state",
        login_state["state"],
        httponly=True,
        samesite="lax",
        max_age=600,
        path="/",
    )
    response.set_cookie(
        "studyagent_oidc_verifier",
        login_state["code_verifier"],
        httponly=True,
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


@router.get("/oidc/callback", response_class=HTMLResponse)
def oidc_callback(code: str, state: str, request: Request, db: DbSession) -> Response:
    _ensure_oidc_enabled()
    expected_state = request.cookies.get("studyagent_oidc_state")
    code_verifier = request.cookies.get("studyagent_oidc_verifier")
    if not expected_state or not code_verifier or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OIDC state")
    try:
        claims = oidc_service.exchange_code_for_claims(code, code_verifier)
        tokens = oidc_service.issue_local_tokens_for_claims(db, claims)
    except OidcAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    web_base = get_settings().web_base_path.rstrip("/") + "/"
    if tokens.get("must_change_password"):
        target = f"{web_base}login"
    else:
        target = f"{web_base}{'student' if tokens.get('role') == 'student' else 'admin'}"
    html = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>统一认证登录中</title></head>
<body>
<p>统一认证成功，正在进入 StudyAgent...</p>
<script>
localStorage.setItem("studyagent-access-token", {tokens["access_token"]!r});
localStorage.setItem("studyagent-refresh-token", {tokens["refresh_token"]!r});
localStorage.setItem("studyagent-sso-session", "1");
location.replace({target!r});
</script>
</body>
</html>"""
    response = HTMLResponse(html)
    response.delete_cookie("studyagent_oidc_state", path="/")
    response.delete_cookie("studyagent_oidc_verifier", path="/")
    return response


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
