from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.models.user import CampusIdentity, User, UserRole


class CampusSsoError(Exception):
    pass


class CampusSsoUnavailable(CampusSsoError):
    pass


@dataclass(frozen=True)
class CampusProfile:
    school_id: str
    external_id: str
    number: str
    name: str
    role: UserRole


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured(settings: Settings) -> bool:
    try:
        url = urlsplit(settings.campus_sso_token_url)
        return bool(
            url.scheme == "https"
            and url.hostname
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and settings.campus_sso_school_id
            and settings.campus_sso_project_id
        )
    except ValueError:
        return False


def verify_campus_token(token: str, settings: Settings) -> CampusProfile:
    if not configured(settings):
        raise CampusSsoUnavailable("中台登录暂未启用")
    if not token or len(token) > 4096 or any(ch.isspace() for ch in token):
        raise CampusSsoError("登录凭证无效，请从中台重新打开 StudyAgent")

    query = urlencode({"cmd": "API", "mode": "getTokenInfo", "cfg": token})
    request = Request(settings.campus_sso_token_url + "?" + query, headers={"Accept": "application/json"})
    try:
        with build_opener(_RejectRedirects()).open(request, timeout=5) as response:
            body = response.read(512 * 1024 + 1)
        if len(body) > 512 * 1024:
            raise ValueError("response too large")
        result = json.loads(body)
    except (URLError, OSError, ValueError):
        raise CampusSsoUnavailable("中台认证服务暂不可用，请稍后重试") from None

    if not isinstance(result, dict) or str(result.get("result")) != "0":
        raise CampusSsoError("登录凭证无效，请从中台重新打开 StudyAgent")
    data = result.get("data")
    if (
        not isinstance(data, dict)
        or data.get("scid") != settings.campus_sso_school_id
        or data.get("projectID") != settings.campus_sso_project_id
    ):
        raise CampusSsoError("该身份未获准通过此入口登录")
    role_code = data.get("userType")
    role = {3: UserRole.STUDENT, 2: UserRole.TEACHER}.get(role_code) if type(role_code) is int else None
    external_id, number, name = data.get("_id"), data.get("number"), data.get("name")
    if role is None or not all(
        isinstance(value, str) and value.strip() and len(value) <= limit
        for value, limit in ((external_id, 128), (number, 64), (name, 64))
    ):
        raise CampusSsoError("该身份未获准通过此入口登录")
    return CampusProfile(settings.campus_sso_school_id, external_id, number, name.strip(), role)


def _bound_user(db: Session, identity: CampusIdentity, profile: CampusProfile) -> User:
    user = db.get(User, identity.user_id)
    if (
        user is None
        or not user.is_active
        or user.graduated_at is not None
        or user.role != profile.role
    ):
        raise CampusSsoError("账号不可用，请联系管理员")
    return user


def resolve_campus_user(db: Session, profile: CampusProfile, *, auto_create_students: bool) -> User:
    school = CampusIdentity.school_id == profile.school_id
    identity = db.scalar(select(CampusIdentity).where(school, CampusIdentity.external_id == profile.external_id))
    if identity is not None:
        return _bound_user(db, identity, profile)

    identity = db.scalar(select(CampusIdentity).where(school, CampusIdentity.number == profile.number))
    try:
        if identity is not None:
            if identity.external_id is not None:
                raise CampusSsoError("中台身份与已有绑定冲突，请联系管理员")
            user = _bound_user(db, identity, profile)
            # Conditional write stops two external identities from claiming the same approved row.
            result = db.execute(
                update(CampusIdentity)
                .where(CampusIdentity.id == identity.id, CampusIdentity.external_id.is_(None))
                .values(external_id=profile.external_id)
            )
            if result.rowcount != 1:
                raise CampusSsoError("中台身份已绑定，请重新从中台打开")
        else:
            if not auto_create_students or profile.role != UserRole.STUDENT:
                raise CampusSsoError("账号尚未完成绑定，请联系管理员")
            if db.scalar(select(User.id).where(User.student_no == profile.number)) is not None:
                raise CampusSsoError("已有账号需要核对身份，请联系管理员")
            identifier = json.dumps([profile.school_id, profile.external_id], ensure_ascii=False)
            username = "campus-" + hashlib.sha256(identifier.encode()).hexdigest()[:48]
            user = User(
                username=username,
                full_name=profile.name,
                role=UserRole.STUDENT,
                password_hash="!campus-sso:" + secrets.token_urlsafe(32),
            )
            db.add(user)
            db.flush()
            db.add(CampusIdentity(
                school_id=profile.school_id,
                external_id=profile.external_id,
                number=profile.number,
                user_id=user.id,
            ))
        db.commit()
    except IntegrityError:
        db.rollback()
        identity = db.scalar(select(CampusIdentity).where(school, CampusIdentity.external_id == profile.external_id))
        if identity is None:
            raise CampusSsoError("中台身份与已有账号冲突，请联系管理员") from None
        return _bound_user(db, identity, profile)
    return user
