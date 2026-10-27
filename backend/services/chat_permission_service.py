"""Time-limited permission to ask questions; account access stays independent."""
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.models.audit_log import AuditLog
from backend.models.user import User, UserRole
from backend.time_utils import assume_utc, to_beijing


def format_chat_ban_message(until: datetime) -> str:
    # The stored instant is exclusive; show the final disabled second to students.
    last_disabled = to_beijing(until) - timedelta(microseconds=1)
    expires = f"{last_disabled.year}年{last_disabled.month}月{last_disabled.day}日 {last_disabled:%H:%M:%S}（北京时间）"
    return (
        "你因违规使用答疑平台 AI 功能，已被禁止问答权限。\n"
        f"封禁期至 {expires}。\n"
        "如有不同意见需要申诉，可以通过意见反馈发送。管理员不一定有时间查看或回复。"
    )


def ensure_chat_allowed(user: User) -> None:
    until = getattr(user, "chat_banned_until", None)
    if user.role == UserRole.STUDENT and until is not None and assume_utc(until) > datetime.now(UTC):
        raise HTTPException(
            status_code=403,
            detail=format_chat_ban_message(until),
        )


def set_student_chat_restriction(
    db: Session,
    *,
    student: User,
    actor: User,
    until: datetime | None,
    reason: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Stage the permission and audit record in one transaction; caller commits."""
    if student.role != UserRole.STUDENT:
        raise HTTPException(status_code=400, detail="只能设置学生的问答权限")
    if until is not None:
        if until.tzinfo is None or until.utcoffset() is None:
            raise HTTPException(status_code=422, detail="截止时间必须包含时区")
        until = assume_utc(until)
        if until <= datetime.now(UTC):
            raise HTTPException(status_code=400, detail="截止时间必须晚于当前时间")
        reason = (reason or "").strip()
        if not reason or len(reason) > 500:
            raise HTTPException(status_code=400, detail="请填写不超过500字的禁用原因")
    previous_until = student.chat_banned_until
    student.chat_banned_until = until
    student.chat_ban_reason = reason if until else None
    db.add(student)
    db.add(AuditLog(
        actor_id=actor.id,
        action="ban_student_chat" if until else "unban_student_chat",
        target_type="user",
        target_id=str(student.id),
        result="success",
        ip_address=ip_address,
        detail={
            "previous_until": assume_utc(previous_until).isoformat() if previous_until else None,
            "until": until.isoformat() if until else None,
            "reason": student.chat_ban_reason,
        },
    ))
