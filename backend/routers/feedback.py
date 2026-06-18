from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.dependencies import CurrentStudent, DbSession
from backend.models.feedback import StudentFeedback, StudentFeedbackBan
from backend.models.schemas import FeedbackCreate, StudentFeedbackListRead, StudentFeedbackRead
from backend.services.audit_service import audit_service

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

DAILY_FEEDBACK_LIMIT = 2
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def today_start_utc() -> datetime:
    now_local = datetime.now(LOCAL_TIMEZONE)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=LOCAL_TIMEZONE)
    return start_local.astimezone(UTC)


def feedback_is_banned(db: DbSession, student_id: int) -> bool:
    return db.scalar(select(StudentFeedbackBan.id).where(StudentFeedbackBan.student_id == student_id).limit(1)) is not None


def today_feedback_count(db: DbSession, student_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(StudentFeedback.id)).where(
                StudentFeedback.student_id == student_id,
                StudentFeedback.created_at >= today_start_utc(),
            )
        )
        or 0
    )


def student_feedback_read(item: StudentFeedback) -> StudentFeedbackRead:
    return StudentFeedbackRead(
        id=item.id,
        content=item.content,
        reply_content=item.reply_content,
        replied_by_name=item.replier.full_name if item.replier else None,
        replied_at=item.replied_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", response_model=StudentFeedbackListRead)
def list_my_feedback(db: DbSession, current_user: CurrentStudent) -> StudentFeedbackListRead:
    items = db.scalars(
        select(StudentFeedback)
        .options(selectinload(StudentFeedback.replier))
        .where(StudentFeedback.student_id == current_user.id)
        .order_by(StudentFeedback.created_at.desc(), StudentFeedback.id.desc())
    ).all()
    count = today_feedback_count(db, current_user.id)
    return StudentFeedbackListRead(
        items=[student_feedback_read(item) for item in items],
        daily_limit=DAILY_FEEDBACK_LIMIT,
        daily_remaining=max(0, DAILY_FEEDBACK_LIMIT - count),
        feedback_banned=feedback_is_banned(db, current_user.id),
    )


@router.post("", response_model=StudentFeedbackRead, status_code=status.HTTP_201_CREATED)
def create_feedback(
    payload: FeedbackCreate,
    db: DbSession,
    current_user: CurrentStudent,
    request: Request,
) -> StudentFeedbackRead:
    if feedback_is_banned(db, current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="反馈权限已被管理员暂停")
    if today_feedback_count(db, current_user.id) >= DAILY_FEEDBACK_LIMIT:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="每天最多提交 2 次反馈，请明天再试")

    item = StudentFeedback(student_id=current_user.id, content=payload.content.strip())
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor=current_user,
        action="create_feedback",
        target_type="student_feedback",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
    )
    return student_feedback_read(item)
