from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from backend.dependencies import CurrentStudent, CurrentTeacher, DbSession
from backend.models.agent_config import AgentConfig
from backend.models.incentive import StudentIncentiveEvent, StudentIncentiveProfile
from backend.models.schemas import (
    IncentivePraiseCreate,
    IncentivePraiseRead,
    IncentiveReflectionPage,
    IncentiveReportRead,
    IncentiveSummaryRead,
    TeacherIncentivePortraitRead,
)
from backend.models.user import User, UserRole, teacher_classes
from backend.services.incentive_service import IncentiveEventDraft, incentive_service
from backend.time_utils import BEIJING_TZ, now_beijing, now_utc

router = APIRouter(prefix="/api/incentive", tags=["incentive"])


def _params(db: DbSession) -> dict[str, Any]:
    active = db.scalar(
        select(AgentConfig).where(AgentConfig.is_active.is_(True)).order_by(AgentConfig.version.desc()).limit(1)
    )
    return incentive_service.resolve_config(active.guidance_params if active else None)


def _teacher_classroom_ids(db: DbSession, current_user: User) -> set[int]:
    if current_user.role == UserRole.ADMIN:
        return set()
    return set(
        db.scalars(
            select(teacher_classes.c.classroom_id).where(teacher_classes.c.teacher_id == current_user.id)
        ).all()
    )


def _student_for_teacher(db: DbSession, current_user: User, student_id: int) -> User:
    student = db.scalar(
        select(User)
        .options(selectinload(User.classroom))
        .where(User.id == student_id, User.role == UserRole.STUDENT)
    )
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")
    if current_user.role != UserRole.ADMIN and student.classroom_id not in _teacher_classroom_ids(db, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Student is outside your classrooms")
    return student


@router.get("/me/summary", response_model=IncentiveSummaryRead)
def my_summary(db: DbSession, current_user: CurrentStudent) -> IncentiveSummaryRead:
    params = _params(db)
    data = incentive_service.get_summary(db, current_user.id, params)
    profile = db.scalar(
        select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == current_user.id)
    )
    latest_praise = db.scalar(
        select(func.max(StudentIncentiveEvent.created_at)).where(
            StudentIncentiveEvent.student_id == current_user.id,
            StudentIncentiveEvent.event_type == "teacher_praise",
        )
    )
    data["has_unread_praise"] = bool(
        latest_praise and (profile is None or profile.last_praise_read_at is None or latest_praise > profile.last_praise_read_at)
    )
    return IncentiveSummaryRead(**data)


@router.get("/me/report", response_model=IncentiveReportRead)
def my_report(
    db: DbSession,
    current_user: CurrentStudent,
    period: str = Query("week", pattern="^(week|month)$"),
) -> IncentiveReportRead:
    return IncentiveReportRead(**incentive_service.get_report(db, current_user.id, period))


@router.get("/me/praises", response_model=list[IncentivePraiseRead])
def my_praises(db: DbSession, current_user: CurrentStudent) -> list[IncentivePraiseRead]:
    events = db.scalars(
        select(StudentIncentiveEvent)
        .where(
            StudentIncentiveEvent.student_id == current_user.id,
            StudentIncentiveEvent.event_type == "teacher_praise",
        )
        .order_by(StudentIncentiveEvent.created_at.desc())
        .limit(50)
    ).all()
    creators = {
        item.id: item.full_name
        for item in db.scalars(select(User).where(User.id.in_({event.created_by for event in events if event.created_by}))).all()
    }
    return [
        IncentivePraiseRead(
            id=event.id,
            content=str((event.payload or {}).get("content") or ""),
            teacher_name=creators.get(event.created_by, "教师"),
            points=event.points,
            created_at=event.created_at,
        )
        for event in events
    ]


@router.post("/me/praises/read", response_model=IncentiveSummaryRead)
def mark_praises_read(db: DbSession, current_user: CurrentStudent) -> IncentiveSummaryRead:
    profile = db.scalar(
        select(StudentIncentiveProfile).where(StudentIncentiveProfile.student_id == current_user.id)
    )
    if profile is None:
        profile = StudentIncentiveProfile(student_id=current_user.id, badges=[], counters={})
    profile.last_praise_read_at = now_utc()
    db.add(profile)
    db.commit()
    return my_summary(db, current_user)


@router.get("/teacher/portraits", response_model=list[TeacherIncentivePortraitRead])
def teacher_portraits(
    db: DbSession,
    current_user: CurrentTeacher,
    classroom_id: int | None = None,
    limit: int = Query(200, ge=1, le=500),
) -> list[TeacherIncentivePortraitRead]:
    statement = (
        select(User, StudentIncentiveProfile)
        .outerjoin(StudentIncentiveProfile, StudentIncentiveProfile.student_id == User.id)
        .options(selectinload(User.classroom))
        .where(User.role == UserRole.STUDENT, User.is_active.is_(True))
    )
    if current_user.role != UserRole.ADMIN:
        allowed = _teacher_classroom_ids(db, current_user)
        if classroom_id is not None and classroom_id not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Classroom is outside your scope")
        statement = statement.where(User.classroom_id.in_(allowed or {-1}))
    if classroom_id is not None:
        statement = statement.where(User.classroom_id == classroom_id)
    rows = db.execute(statement.order_by(User.classroom_id, User.full_name).limit(limit)).all()
    since = now_utc() - timedelta(days=7)
    student_ids = [student.id for student, _ in rows]
    activity_by_student: dict[int, tuple[int, Any]] = {}
    learning_days_by_student: dict[int, set[Any]] = {}
    if student_ids:
        activity_rows = db.execute(
            select(
                StudentIncentiveEvent.student_id,
                func.sum(
                    case(
                        (
                            (StudentIncentiveEvent.event_type == "followup_answered")
                            & (StudentIncentiveEvent.created_at >= since),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.max(
                    case(
                        (StudentIncentiveEvent.event_type == "teacher_praise", StudentIncentiveEvent.created_at),
                        else_=None,
                    )
                ),
            )
            .where(StudentIncentiveEvent.student_id.in_(student_ids))
            .group_by(StudentIncentiveEvent.student_id)
        ).all()
        activity_by_student = {
            student_id: (int(weekly_followups or 0), last_praise_at)
            for student_id, weekly_followups, last_praise_at in activity_rows
        }
        learning_rows = db.execute(
            select(
                StudentIncentiveEvent.student_id,
                StudentIncentiveEvent.event_type,
                StudentIncentiveEvent.created_at,
                StudentIncentiveEvent.payload,
            ).where(
                StudentIncentiveEvent.student_id.in_(student_ids),
                StudentIncentiveEvent.event_type.in_([
                    "followup_answered",
                    "practice_passed",
                    "early_resolved",
                    "resolved_after_fallback",
                ]),
                StudentIncentiveEvent.created_at >= since,
            )
        ).all()
        for student_id, event_type, created_at, payload in learning_rows:
            if event_type == "followup_answered" and not bool((payload or {}).get("valid_learning", True)):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=now_utc().tzinfo)
            learning_days_by_student.setdefault(student_id, set()).add(
                created_at.astimezone(BEIJING_TZ).date()
            )
    result: list[TeacherIncentivePortraitRead] = []
    for student, profile in rows:
        counts = dict(profile.counters or {}) if profile else {}
        weekly_followups, last_praise_at = activity_by_student.get(student.id, (0, None))
        result.append(
            TeacherIncentivePortraitRead(
                student_id=student.id,
                student_name=student.full_name,
                classroom_label=student.classroom_label,
                total_points=profile.total_points if profile else 0,
                level=profile.level if profile else 1,
                current_streak_days=profile.current_streak_days if profile else 0,
                weekly_learning_days=len(learning_days_by_student.get(student.id, set())),
                weekly_followups=weekly_followups,
                quality_resolves=int(counts.get("early_resolved", 0))
                + int(counts.get("resolved_after_fallback", 0)),
                last_praise_at=last_praise_at,
            )
        )
    return result


@router.get("/teacher/students/{student_id}/reflections", response_model=IncentiveReflectionPage)
def teacher_student_reflections(
    student_id: int,
    db: DbSession,
    current_user: CurrentTeacher,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> IncentiveReflectionPage:
    student = _student_for_teacher(db, current_user, student_id)
    condition = (
        StudentIncentiveEvent.student_id == student_id,
        StudentIncentiveEvent.event_type == "reflection_submitted",
    )
    total = db.scalar(select(func.count(StudentIncentiveEvent.id)).where(*condition)) or 0
    events = db.scalars(
        select(StudentIncentiveEvent)
        .where(*condition)
        .order_by(StudentIncentiveEvent.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return IncentiveReflectionPage(
        student_id=student.id,
        student_name=student.full_name,
        items=[
            {
                "id": event.id,
                "conversation_id": event.conversation_id,
                "subject": event.subject,
                "reflection": str((event.payload or {}).get("reflection") or ""),
                "created_at": event.created_at,
            }
            for event in events
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/teacher/praise", response_model=IncentivePraiseRead, status_code=status.HTTP_201_CREATED)
def teacher_praise(
    payload: IncentivePraiseCreate,
    db: DbSession,
    current_user: CurrentTeacher,
) -> IncentivePraiseRead:
    student = _student_for_teacher(db, current_user, payload.student_id)
    params = _params(db)
    now = now_beijing()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now_utc().tzinfo)
    end = start + timedelta(days=1)
    duplicate = db.scalar(
        select(func.count(StudentIncentiveEvent.id)).where(
            StudentIncentiveEvent.student_id == student.id,
            StudentIncentiveEvent.created_by == current_user.id,
            StudentIncentiveEvent.event_type == "teacher_praise",
            StudentIncentiveEvent.created_at >= start,
            StudentIncentiveEvent.created_at < end,
        )
    ) or 0
    teacher_total = db.scalar(
        select(func.count(StudentIncentiveEvent.id)).where(
            StudentIncentiveEvent.created_by == current_user.id,
            StudentIncentiveEvent.event_type == "teacher_praise",
            StudentIncentiveEvent.created_at >= start,
            StudentIncentiveEvent.created_at < end,
        )
    ) or 0
    if duplicate:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="今天已经表扬过这位学生")
    if teacher_total >= params["teacher_praise_daily_cap"]:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="今天的表扬次数已达上限")
    draft = IncentiveEventDraft(
        event_type="teacher_praise",
        points=params["points"]["teacher_praise"],
        dedup_key=f"teacher:{current_user.id}:student:{student.id}:praise:{now.date().isoformat()}",
        payload={"content": payload.content.strip()},
        created_by=current_user.id,
    )
    grant = incentive_service.record_events(db, student_id=student.id, drafts=[draft], params=params, occurred_at=now)
    db.commit()
    event = db.scalar(select(StudentIncentiveEvent).where(StudentIncentiveEvent.dedup_key == draft.dedup_key))
    return IncentivePraiseRead(
        id=event.id,
        content=payload.content.strip(),
        teacher_name=current_user.full_name,
        points=grant.points_awarded,
        created_at=event.created_at,
    )
