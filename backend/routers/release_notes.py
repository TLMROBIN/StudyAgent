from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from backend.dependencies import CurrentStudent, DbSession
from backend.models.feedback import ReleaseNote, ReleaseNoteReadState
from backend.models.schemas import ReleaseNoteListRead, ReleaseNoteRead
from backend.routers.feedback import read_at_covers

router = APIRouter(prefix="/api/release-notes", tags=["release-notes"])


def release_note_is_read(db: DbSession, note: ReleaseNote, student_id: int) -> bool:
    if not note.published_at:
        return True
    state = db.scalar(
        select(ReleaseNoteReadState).where(
            ReleaseNoteReadState.student_id == student_id,
            ReleaseNoteReadState.release_note_id == note.id,
        )
    )
    return state is not None and read_at_covers(state.read_at, note.published_at)


def release_note_read(db: DbSession, note: ReleaseNote, student_id: int) -> ReleaseNoteRead:
    return ReleaseNoteRead(
        id=note.id,
        title=note.title,
        content=note.content,
        is_published=note.is_published,
        is_read=release_note_is_read(db, note, student_id),
        published_at=note.published_at,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


@router.get("", response_model=ReleaseNoteListRead)
def list_release_notes(db: DbSession, current_user: CurrentStudent) -> ReleaseNoteListRead:
    notes = db.scalars(
        select(ReleaseNote)
        .where(ReleaseNote.is_published.is_(True), ReleaseNote.published_at.is_not(None))
        .order_by(ReleaseNote.published_at.desc(), ReleaseNote.id.desc())
    ).all()
    items = [release_note_read(db, note, current_user.id) for note in notes]
    return ReleaseNoteListRead(
        items=items,
        unread_count=sum(1 for item in items if not item.is_read),
    )


@router.post("/{note_id}/read", response_model=ReleaseNoteRead)
def mark_release_note_read(note_id: int, db: DbSession, current_user: CurrentStudent) -> ReleaseNoteRead:
    note = db.scalar(
        select(ReleaseNote).where(
            ReleaseNote.id == note_id,
            ReleaseNote.is_published.is_(True),
            ReleaseNote.published_at.is_not(None),
        )
    )
    if not note:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release note not found")
    now = datetime.now(UTC)
    state = db.scalar(
        select(ReleaseNoteReadState).where(
            ReleaseNoteReadState.student_id == current_user.id,
            ReleaseNoteReadState.release_note_id == note.id,
        )
    )
    if state:
        state.read_at = now
    else:
        state = ReleaseNoteReadState(student_id=current_user.id, release_note_id=note.id, read_at=now)
    db.add(state)
    db.commit()
    db.refresh(note)
    return release_note_read(db, note, current_user.id)
