from __future__ import annotations

from datetime import UTC, datetime, time
import mimetypes
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from starlette.datastructures import UploadFile as StarletteUploadFile

from backend.dependencies import CurrentStudent, CurrentUser, DbSession
from backend.models.feedback import (
    ReleaseNote,
    ReleaseNoteReadState,
    StudentFeedback,
    StudentFeedbackAttachment,
    StudentFeedbackBan,
    StudentFeedbackReadState,
)
from backend.models.schemas import FeedbackCreate, FeedbackUnreadSummaryRead, StudentFeedbackListRead, StudentFeedbackRead
from backend.models.user import UserRole
from backend.services.audit_service import audit_service
from backend.services.feedback_attachment_service import StoredFeedbackAttachment, feedback_attachment_service

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

DAILY_FEEDBACK_LIMIT = 2
MAX_FEEDBACK_IMAGES = 6
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def read_at_covers(read_at: datetime, changed_at: datetime) -> bool:
    return _as_utc(read_at) >= _as_utc(changed_at)


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


def feedback_reply_is_unread(db: DbSession, item: StudentFeedback, student_id: int) -> bool:
    if not item.reply_content or not item.replied_at:
        return False
    state = db.scalar(
        select(StudentFeedbackReadState).where(
            StudentFeedbackReadState.student_id == student_id,
            StudentFeedbackReadState.feedback_id == item.id,
        )
    )
    return state is None or not read_at_covers(state.read_at, item.replied_at)


def unread_feedback_reply_count(db: DbSession, student_id: int) -> int:
    items = db.scalars(
        select(StudentFeedback).where(
            StudentFeedback.student_id == student_id,
            StudentFeedback.reply_content.is_not(None),
            StudentFeedback.replied_at.is_not(None),
        )
    ).all()
    return sum(1 for item in items if feedback_reply_is_unread(db, item, student_id))


def unread_release_note_count(db: DbSession, student_id: int) -> int:
    notes = db.scalars(
        select(ReleaseNote).where(
            ReleaseNote.is_published.is_(True),
            ReleaseNote.published_at.is_not(None),
        )
    ).all()
    total = 0
    for note in notes:
        state = db.scalar(
            select(ReleaseNoteReadState).where(
                ReleaseNoteReadState.student_id == student_id,
                ReleaseNoteReadState.release_note_id == note.id,
            )
        )
        if state is None or (note.published_at and state.read_at < note.published_at):
            total += 1
    return total


def _feedback_attachment_read(item: StudentFeedbackAttachment) -> dict[str, Any]:
    return {
        "asset_id": item.asset_id,
        "attachment_id": item.attachment_id,
        "filename": item.filename,
        "content_type": item.content_type,
        "url": item.url,
        "size_bytes": item.size_bytes,
    }


def student_feedback_read(db: DbSession, item: StudentFeedback, student_id: int) -> StudentFeedbackRead:
    return StudentFeedbackRead(
        id=item.id,
        content=item.content,
        attachments=[_feedback_attachment_read(attachment) for attachment in item.attachments],
        reply_content=item.reply_content,
        replied_by_name=item.replier.full_name if item.replier else None,
        replied_at=item.replied_at,
        student_unread=feedback_reply_is_unread(db, item, student_id),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", response_model=StudentFeedbackListRead)
def list_my_feedback(db: DbSession, current_user: CurrentStudent) -> StudentFeedbackListRead:
    items = db.scalars(
        select(StudentFeedback)
        .options(selectinload(StudentFeedback.replier), selectinload(StudentFeedback.attachments))
        .where(StudentFeedback.student_id == current_user.id)
        .order_by(StudentFeedback.created_at.desc(), StudentFeedback.id.desc())
    ).all()
    count = today_feedback_count(db, current_user.id)
    unread_count = unread_feedback_reply_count(db, current_user.id)
    return StudentFeedbackListRead(
        items=[student_feedback_read(db, item, current_user.id) for item in items],
        daily_limit=DAILY_FEEDBACK_LIMIT,
        daily_remaining=max(0, DAILY_FEEDBACK_LIMIT - count),
        feedback_banned=feedback_is_banned(db, current_user.id),
        unread_reply_count=unread_count,
    )


@router.get("/unread-summary", response_model=FeedbackUnreadSummaryRead)
def unread_summary(db: DbSession, current_user: CurrentStudent) -> FeedbackUnreadSummaryRead:
    feedback_count = unread_feedback_reply_count(db, current_user.id)
    release_note_count = unread_release_note_count(db, current_user.id)
    return FeedbackUnreadSummaryRead(
        unread_feedback_replies=feedback_count,
        unread_release_notes=release_note_count,
        has_unread=feedback_count + release_note_count > 0,
    )


@router.post("/{feedback_id}/read", response_model=StudentFeedbackRead)
def mark_feedback_read(feedback_id: int, db: DbSession, current_user: CurrentStudent) -> StudentFeedbackRead:
    item = db.scalar(
        select(StudentFeedback)
        .options(selectinload(StudentFeedback.replier), selectinload(StudentFeedback.attachments))
        .where(StudentFeedback.id == feedback_id, StudentFeedback.student_id == current_user.id)
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    now = datetime.now(UTC)
    state = db.scalar(
        select(StudentFeedbackReadState).where(
            StudentFeedbackReadState.student_id == current_user.id,
            StudentFeedbackReadState.feedback_id == item.id,
        )
    )
    if state:
        state.read_at = now
    else:
        state = StudentFeedbackReadState(student_id=current_user.id, feedback_id=item.id, read_at=now)
    db.add(state)
    db.commit()
    db.refresh(item)
    return student_feedback_read(db, item, current_user.id)


async def _parse_feedback_submission(request: Request) -> tuple[FeedbackCreate, list[UploadFile]]:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        content = str(form.get("content") or "")
        raw_files = form.getlist("images")
        files = [item for item in raw_files if isinstance(item, (UploadFile, StarletteUploadFile)) and item.filename]
        try:
            return FeedbackCreate(content=content), files
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()) from exc
    if content_type.startswith("application/x-www-form-urlencoded"):
        form = await request.form()
        try:
            return FeedbackCreate(content=str(form.get("content") or "")), []
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()) from exc

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid feedback payload") from exc
    try:
        return FeedbackCreate.model_validate(payload), []
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()) from exc


async def _read_feedback_images(files: list[UploadFile]) -> list[tuple[UploadFile, bytes]]:
    if len(files) > MAX_FEEDBACK_IMAGES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"每次最多上传 {MAX_FEEDBACK_IMAGES} 张图片")
    images: list[tuple[UploadFile, bytes]] = []
    for file in files:
        images.append((file, await file.read()))
    return images


@router.post("", response_model=StudentFeedbackRead, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    db: DbSession,
    current_user: CurrentStudent,
    request: Request,
) -> StudentFeedbackRead:
    payload, files = await _parse_feedback_submission(request)
    if feedback_is_banned(db, current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="反馈权限已被管理员暂停")
    if today_feedback_count(db, current_user.id) >= DAILY_FEEDBACK_LIMIT:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="每天最多提交 2 次反馈，请明天再试")

    images = await _read_feedback_images(files)
    item = StudentFeedback(student_id=current_user.id, content=payload.content.strip())
    saved_attachments: list[StoredFeedbackAttachment] = []
    db.add(item)
    try:
        db.flush()
        for file, content in images:
            stored = feedback_attachment_service.save_bytes(
                content=content,
                filename=file.filename or "feedback-image",
                content_type=file.content_type,
                student_id=current_user.id,
                feedback_id=item.id,
            )
            saved_attachments.append(stored)
            db.add(
                StudentFeedbackAttachment(
                    feedback_id=item.id,
                    student_id=current_user.id,
                    storage_key=stored.storage_key,
                    original_filename=stored.original_filename,
                    mime_type=stored.mime_type,
                    file_size=stored.file_size,
                    sha256=stored.sha256,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        for stored in saved_attachments:
            feedback_attachment_service.delete(stored.storage_key)
        raise
    item = db.scalars(
        select(StudentFeedback)
        .options(selectinload(StudentFeedback.replier), selectinload(StudentFeedback.attachments))
        .where(StudentFeedback.id == item.id)
    ).one()
    audit_service.log(
        db,
        actor=current_user,
        action="create_feedback",
        target_type="student_feedback",
        target_id=str(item.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={"attachment_count": len(saved_attachments)},
    )
    return student_feedback_read(db, item, current_user.id)


@router.get("/attachments/{attachment_id}")
def get_feedback_attachment(attachment_id: int, db: DbSession, current_user: CurrentUser):
    attachment = db.scalar(
        select(StudentFeedbackAttachment)
        .options(selectinload(StudentFeedbackAttachment.feedback))
        .where(StudentFeedbackAttachment.id == attachment_id)
    )
    if not attachment or (current_user.role != UserRole.ADMIN and attachment.student_id != current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    file_path = feedback_attachment_service.resolve_path(attachment.storage_key)
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    media_type = mimetypes.guess_type(file_path.name)[0] or attachment.mime_type or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type, filename=attachment.original_filename)
