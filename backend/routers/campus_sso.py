"""Browser handoff for the school's token-based login."""
from dataclasses import asdict
from html import escape
import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.config import Settings, get_settings
from backend.dependencies import DbSession
from backend.models.user import UserRole
from backend.services.auth_service import auth_service
from backend.services.campus_sso import (
    CampusProfile,
    CampusSsoError,
    CampusSsoUnavailable,
    configured,
    resolve_campus_user,
    verify_campus_token,
)
from backend.services.store_service import store

router = APIRouter(prefix="/sso", tags=["auth"])
_COOKIE = "studyagent_campus_sso"
_PENDING_SECONDS = 120
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}


def _base() -> str:
    return get_settings().web_base_path.rstrip("/")


def _confirm_path() -> str:
    return f"{_base()}/sso/confirm"


def _page(content: str, *, status_code: int = 200, nonce: str | None = None) -> HTMLResponse:
    policy = "default-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    if nonce:
        policy += f"; script-src 'nonce-{nonce}'"
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>StudyAgent 中台登录</title><main><h1>StudyAgent 中台登录</h1>'
        f'{content}</main></html>',
        status_code=status_code,
        headers={**_HEADERS, "Content-Security-Policy": policy},
    )


def _unavailable() -> HTMLResponse:
    return _page("<p>中台登录暂不可用，请从原统一平台进入。</p>", status_code=503)


def _can_accept(request: Request, settings: Settings) -> bool:
    scheme = request.url.scheme
    return configured(settings) and (scheme == "https" or (scheme == "http" and settings.campus_sso_allow_http))


@router.get("", include_in_schema=False, response_model=None)
def campus_callback(request: Request) -> HTMLResponse | RedirectResponse:
    settings = get_settings()
    if not _can_accept(request, settings):
        return _unavailable()
    secure = request.url.scheme == "https"

    token_values = request.query_params.getlist("token")
    token = token_values[0] if len(token_values) == 1 else ""
    try:
        profile = verify_campus_token(token, settings)
    except CampusSsoError as exc:
        error = "unavailable" if isinstance(exc, CampusSsoUnavailable) else "invalid"
        response = RedirectResponse(f"sso/confirm?error={error}", status_code=303, headers=_HEADERS)
        response.delete_cookie(_COOKIE, path=_confirm_path(), secure=secure, httponly=True, samesite="lax")
        return response

    ticket = secrets.token_urlsafe(32)
    store.set(f"campus-sso:pending:{ticket}", json.dumps(asdict(profile)), ttl_seconds=_PENDING_SECONDS)
    response = RedirectResponse("sso/confirm", status_code=303, headers=_HEADERS)
    response.set_cookie(
        _COOKIE, ticket, max_age=_PENDING_SECONDS, path=_confirm_path(),
        secure=secure, httponly=True, samesite="lax",
    )
    return response


@router.get("/confirm", include_in_schema=False)
def campus_confirmation(request: Request) -> HTMLResponse:
    if not _can_accept(request, get_settings()):
        return _unavailable()
    if request.query_params.get("error") == "unavailable":
        return _unavailable()
    ticket = request.cookies.get(_COOKIE, "")
    pending = store.get(f"campus-sso:pending:{ticket}") if ticket else None
    if not pending or request.query_params.get("error"):
        return _page("<p>登录请求已失效，请从中台重新打开 StudyAgent。</p>", status_code=401)

    name = escape(json.loads(pending)["name"])
    return _page(
        f"<p>以 <strong>{name}</strong> 的身份进入 StudyAgent。</p>"
        '<form method="post">'
        f'<input type="hidden" name="confirmation" value="{escape(ticket, quote=True)}">'
        '<button type="submit">进入 StudyAgent</button></form>'
    )


@router.post("/confirm", include_in_schema=False)
def campus_complete(request: Request, db: DbSession, confirmation: Annotated[str, Form()] = "") -> HTMLResponse:
    settings = get_settings()
    if not _can_accept(request, settings):
        return _unavailable()
    ticket = request.cookies.get(_COOKIE, "")
    if not ticket or not secrets.compare_digest(ticket.encode(), confirmation.encode()):
        return _page("<p>请从中台重新打开 StudyAgent。</p>", status_code=403)
    key = f"campus-sso:pending:{ticket}"
    pending = store.take(key)
    if not pending:
        return _page("<p>登录请求已失效，请从中台重新打开 StudyAgent。</p>", status_code=401)
    record = json.loads(pending)
    profile = CampusProfile(**{**record, "role": UserRole(record["role"])})
    try:
        user = resolve_campus_user(
            db, profile,
            auto_create_students=settings.campus_sso_auto_create_students,
            auto_create_teachers=settings.campus_sso_auto_create_teachers,
            allow_unlisted_students=settings.campus_sso_allow_unlisted_students,
            blocked_student_numbers=frozenset(
                number.strip() for number in settings.campus_sso_blocked_student_numbers.split(",") if number.strip()
            ),
        )
    except CampusSsoError as exc:
        response = _page(f"<p>{escape(str(exc))}</p>", status_code=403)
    else:
        tokens = auth_service.issue_token_pair(user)
        target = f"{_base()}/{'student' if user.role == UserRole.STUDENT else 'admin'}"
        nonce = secrets.token_urlsafe(24)
        response = _page(
            '<p>正在进入 StudyAgent…</p>'
            f'<script nonce="{nonce}">'
            f'localStorage.setItem("studyagent-access-token", {json.dumps(tokens["access_token"])});'
            f'localStorage.setItem("studyagent-refresh-token", {json.dumps(tokens["refresh_token"])});'
            'localStorage.removeItem("studyagent-id-token-hint");'
            'localStorage.removeItem("studyagent-sso-session");'
            'localStorage.setItem("studyagent-campus-sso-session", "1");'
            f'location.replace({json.dumps(target)});</script>',
            nonce=nonce,
        )
    response.delete_cookie(
        _COOKIE, path=_confirm_path(), secure=request.url.scheme == "https", httponly=True, samesite="lax",
    )
    return response
