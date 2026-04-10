from __future__ import annotations

import mimetypes
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from backend.config import get_settings
from backend.dependencies import CurrentTeacher, CurrentUser, DbSession
from backend.models.knowledge import DifficultyLevel, DocumentStatus, ImportTask, KnowledgeChunk, KnowledgeDocument, ResourceType
from backend.models.schemas import (
    ImportTaskRead,
    KnowledgeChunkRead,
    KnowledgeDocumentBulkUpdate,
    KnowledgeDocumentRead,
    KnowledgeDocumentUpdate,
)
from backend.services.audit_service import audit_service
from backend.services.rag_service import rag_service
from backend.tasks.celery_app import celery_app
from backend.tasks.ingest import dispatch_import_task, dispatch_next_pdf_task, is_pdf_queue_waiting_message, sync_task_state
from backend.time_utils import now_beijing

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
settings = get_settings()
ACTIVE_TASK_STATUSES = {DocumentStatus.PENDING, DocumentStatus.PROCESSING}
QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}


def _validate_upload(file: UploadFile) -> None:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.upload_extension_list:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported file type")

    content_type = (file.content_type or "").lower()
    if content_type and content_type not in settings.upload_mime_type_list:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported MIME type")


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_tags(raw_tags: str | list[str] | None) -> list[str]:
    if raw_tags is None:
        return []
    values = raw_tags if isinstance(raw_tags, list) else raw_tags.replace("，", ",").replace("\n", ",").split(",")
    tags: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = str(item).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(normalized[:32])
    return tags[:20]


def _apply_document_metadata(
    document: KnowledgeDocument,
    *,
    resource_type: ResourceType | str,
    grade: int | None,
    chapter: str | None,
    section: str | None,
    difficulty: DifficultyLevel | str | None,
    tags: str | list[str] | None,
) -> None:
    resource_type_value = resource_type.value if isinstance(resource_type, ResourceType) else str(resource_type).strip()
    if resource_type_value not in {item.value for item in ResourceType}:
        resource_type_value = ResourceType.KNOWLEDGE_NOTE.value

    difficulty_value = difficulty.value if isinstance(difficulty, DifficultyLevel) else _normalize_optional_text(difficulty)
    if difficulty_value and difficulty_value not in {item.value for item in DifficultyLevel}:
        difficulty_value = None
    if resource_type_value not in QUESTION_RESOURCE_TYPES:
        difficulty_value = None

    if resource_type_value == ResourceType.EXTENSION.value:
        chapter = None
        section = None

    document.resource_type = resource_type_value
    document.grade = grade
    document.chapter = _normalize_optional_text(chapter)
    document.section = _normalize_optional_text(section)
    document.difficulty = difficulty_value
    document.tags_json = _normalize_tags(tags)


def _merge_document_metadata(document: KnowledgeDocument, changes: dict) -> None:
    _apply_document_metadata(
        document,
        resource_type=changes.get("resource_type", document.resource_type or ResourceType.KNOWLEDGE_NOTE.value),
        grade=changes.get("grade", document.grade),
        chapter=changes.get("chapter", document.chapter),
        section=changes.get("section", document.section),
        difficulty=changes.get("difficulty", document.difficulty),
        tags=changes.get("tags", document.tags),
    )


def _document_detail(document: KnowledgeDocument) -> dict:
    return {
        "filename": document.filename,
        "subject": document.subject,
        "resource_type": document.resource_type,
        "grade": document.grade,
        "chapter": document.chapter,
        "section": document.section,
        "difficulty": document.difficulty,
        "tags": document.tags,
    }


def _chunk_read(row: KnowledgeChunk) -> KnowledgeChunkRead:
    metadata = row.metadata_json or {}
    document = row.document
    return KnowledgeChunkRead(
        id=row.id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        subject=row.subject,
        resource_type=str(metadata.get("resource_type") or (document.resource_type if document else "") or ""),
        grade=metadata.get("grade") or (document.grade if document else None),
        chapter=metadata.get("chapter") or (document.chapter if document else None),
        section=metadata.get("section") or (document.section if document else None),
        difficulty=metadata.get("difficulty") or (document.difficulty if document else None),
        tags=list(metadata.get("tags") or (document.tags if document else [])),
        chunk_kind=metadata.get("chunk_kind"),
        question_number=metadata.get("question_number"),
        question_text=metadata.get("question_text"),
        answer_text=metadata.get("answer_text"),
        explanation_text=metadata.get("explanation_text"),
        contains_images=bool(metadata.get("contains_images")),
        image_count=int(metadata.get("image_count") or 0),
        assets=list(metadata.get("asset_refs") or []),
    )


def _sync_document_state(document: KnowledgeDocument, db: DbSession) -> KnowledgeDocument:
    latest_task = db.scalar(
        select(ImportTask)
        .where(ImportTask.document_id == document.id)
        .order_by(ImportTask.updated_at.desc())
        .limit(1)
    )
    if latest_task:
        latest_task = sync_task_state(latest_task, db)
        if document.status != latest_task.status or document.error_message != latest_task.error_message:
            document.status = latest_task.status
            document.error_message = latest_task.error_message
            db.add(document)
            db.commit()
            db.refresh(document)
        return document

    if document.status in ACTIVE_TASK_STATUSES:
        document.status = DocumentStatus.FAILED
        document.error_message = document.error_message or "未找到关联导入任务，可删除后重新上传"
        db.add(document)
        db.commit()
        db.refresh(document)
    return document


@router.get("/documents", response_model=list[KnowledgeDocumentRead])
def list_documents(db: DbSession, current_user: CurrentTeacher) -> list[KnowledgeDocumentRead]:
    dispatch_next_pdf_task(db)
    documents = db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc())).all()
    return [KnowledgeDocumentRead.model_validate(_sync_document_state(item, db)) for item in documents]


@router.get("/documents/{document_id}/chunks", response_model=list[KnowledgeChunkRead])
def list_document_chunks(document_id: int, db: DbSession, current_user: CurrentTeacher) -> list[KnowledgeChunkRead]:
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    rows = db.scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == document_id)
        .order_by(KnowledgeChunk.chunk_index.asc())
    ).all()
    return [_chunk_read(row) for row in rows]


@router.get("/documents/{document_id}/assets/{asset_name}")
def get_document_asset(document_id: int, asset_name: str, db: DbSession, current_user: CurrentUser):
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    asset_dir = rag_service.document_asset_dir(document_id).resolve()
    asset_path = (asset_dir / asset_name).resolve()
    if asset_dir not in asset_path.parents or not asset_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    media_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
    return FileResponse(asset_path, media_type=media_type, filename=asset_path.name)


@router.post("/upload", response_model=ImportTaskRead, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background_tasks: BackgroundTasks,
    subject: str,
    resource_type: str = Form(ResourceType.KNOWLEDGE_NOTE.value),
    grade: int | None = Form(None),
    chapter: str | None = Form(None),
    section: str | None = Form(None),
    difficulty: str | None = Form(None),
    tags: str | None = Form(None),
    file: UploadFile = File(...),
    db: DbSession = None,
    current_user: CurrentTeacher = None,
    request: Request = None,
) -> ImportTaskRead:
    _validate_upload(file)
    content = await file.read()
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File too large")

    saved_name = f"{now_beijing().strftime('%Y%m%d%H%M%S')}_{uuid4().hex}{Path(file.filename or '').suffix.lower()}"
    target_path = Path(settings.upload_path) / saved_name
    target_path.write_bytes(content)

    document = KnowledgeDocument(
        subject=subject,
        filename=file.filename or saved_name,
        file_path=str(target_path),
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=len(content),
        status=DocumentStatus.PENDING,
        created_by=current_user.id,
    )
    _apply_document_metadata(
        document,
        resource_type=resource_type,
        grade=grade,
        chapter=chapter,
        section=section,
        difficulty=difficulty,
        tags=tags,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    task = ImportTask(document_id=document.id, status=DocumentStatus.PENDING, progress=0, error_message="任务已创建，等待分配 worker")
    db.add(task)
    db.commit()
    db.refresh(task)

    dispatch_import_task(db, document, task, background_tasks=background_tasks)
    db.refresh(task)

    audit_service.log(
        db,
        actor=current_user,
        action="knowledge_upload",
        target_type="knowledge_document",
        target_id=str(document.id),
        result="accepted",
        ip_address=request.client.host if request and request.client else None,
        detail=_document_detail(document),
    )
    return ImportTaskRead.model_validate(task)


@router.put("/documents/bulk", response_model=list[KnowledgeDocumentRead])
def bulk_update_documents(
    payload: KnowledgeDocumentBulkUpdate,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> list[KnowledgeDocumentRead]:
    document_ids = list(dict.fromkeys(payload.document_ids))
    documents = db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids))).all()
    document_map = {document.id: document for document in documents}
    missing_ids = [document_id for document_id in document_ids if document_id not in document_map]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Documents not found: {', '.join(str(item) for item in missing_ids[:10])}",
        )

    ordered_documents = [document_map[document_id] for document_id in document_ids]
    active_documents: list[str] = []
    for document in ordered_documents:
        related_tasks = [sync_task_state(task, db) for task in list(document.tasks)]
        if any(task.status in ACTIVE_TASK_STATUSES for task in related_tasks):
            active_documents.append(document.filename)
    if active_documents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Documents have active import tasks: {', '.join(active_documents[:5])}",
        )

    changes = payload.model_dump(exclude={"document_ids"}, exclude_unset=True, mode="json")
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No metadata changes provided")

    for document in ordered_documents:
        _merge_document_metadata(document, changes)
        db.add(document)
    db.commit()

    updated_documents: list[KnowledgeDocument] = []
    for document in ordered_documents:
        db.refresh(document)
        rag_service.sync_document_metadata(db, document)
        db.refresh(document)
        updated_documents.append(document)

    audit_service.log(
        db,
        actor=current_user,
        action="bulk_update_knowledge_documents",
        target_type="knowledge_document",
        target_id=",".join(str(item) for item in document_ids[:50]),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={
            "document_count": len(updated_documents),
            "updated_fields": sorted(changes.keys()),
            "resource_type": changes.get("resource_type"),
            "grade": changes.get("grade"),
            "chapter": changes.get("chapter"),
            "section": changes.get("section"),
            "difficulty": changes.get("difficulty"),
            "tags": changes.get("tags"),
        },
    )
    return [KnowledgeDocumentRead.model_validate(document) for document in updated_documents]


@router.put("/documents/{document_id}", response_model=KnowledgeDocumentRead)
def update_document(
    document_id: int,
    payload: KnowledgeDocumentUpdate,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> KnowledgeDocumentRead:
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    related_tasks = [sync_task_state(task, db) for task in list(document.tasks)]
    if any(task.status in ACTIVE_TASK_STATUSES for task in related_tasks):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document has active import task")

    _merge_document_metadata(document, payload.model_dump(mode="json"))
    db.add(document)
    db.commit()
    db.refresh(document)
    rag_service.sync_document_metadata(db, document)
    db.refresh(document)
    audit_service.log(
        db,
        actor=current_user,
        action="update_knowledge_document",
        target_type="knowledge_document",
        target_id=str(document.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail=_document_detail(document),
    )
    return KnowledgeDocumentRead.model_validate(document)


@router.get("/tasks", response_model=list[ImportTaskRead])
def list_tasks(limit: int = 100, db: DbSession = None, current_user: CurrentTeacher = None) -> list[ImportTaskRead]:
    dispatch_next_pdf_task(db)
    tasks = db.scalars(select(ImportTask).order_by(ImportTask.updated_at.desc()).limit(limit)).all()
    return [ImportTaskRead.model_validate(sync_task_state(task, db)) for task in tasks]


@router.get("/tasks/{task_id}", response_model=ImportTaskRead)
def get_task(task_id: int, db: DbSession, current_user: CurrentTeacher) -> ImportTaskRead:
    dispatch_next_pdf_task(db)
    task = db.get(ImportTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    task = sync_task_state(task, db)
    return ImportTaskRead.model_validate(task)


@router.post("/tasks/{task_id}/cancel", response_model=ImportTaskRead)
def cancel_task(task_id: int, db: DbSession, current_user: CurrentTeacher) -> ImportTaskRead:
    task = db.get(ImportTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.CANCELLED}:
        return ImportTaskRead.model_validate(task)
    was_waiting_pdf = (
        task.document is not None
        and str(task.document.mime_type or "").lower() == "application/pdf"
        and task.status == DocumentStatus.PENDING
        and is_pdf_queue_waiting_message(task.error_message)
    )
    task.status = DocumentStatus.CANCELLED
    task.error_message = "已请求取消任务"
    if task.document:
        task.document.status = DocumentStatus.CANCELLED
        task.document.error_message = "已请求取消任务"
    if task.celery_task_id:
        celery_app.control.revoke(task.celery_task_id, terminate=False)
    db.add(task)
    db.commit()
    db.refresh(task)
    if was_waiting_pdf:
        dispatch_next_pdf_task(db)
        db.refresh(task)
    audit_service.log(
        db,
        actor=current_user,
        action="cancel_knowledge_task",
        target_type="import_task",
        target_id=str(task.id),
        result="success",
        ip_address=None,
        detail={"document_id": task.document_id, "status": task.status.value},
    )
    return ImportTaskRead.model_validate(task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_200_OK)
def delete_task(
    task_id: int,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> None:
    task = db.get(ImportTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    task = sync_task_state(task, db)
    if task.status in ACTIVE_TASK_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Active task cannot be deleted")

    detail = {
        "document_id": task.document_id,
        "status": task.status.value,
        "filename": task.document_filename,
        "subject": task.document_subject,
    }
    db.delete(task)
    db.commit()
    audit_service.log(
        db,
        actor=current_user,
        action="delete_knowledge_task",
        target_type="import_task",
        target_id=str(task_id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail=detail,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_200_OK)
def delete_document(
    document_id: int,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> None:
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    related_tasks = [sync_task_state(task, db) for task in list(document.tasks)]
    active_tasks = [task for task in related_tasks if task.status in ACTIVE_TASK_STATUSES]
    if active_tasks:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document has active import task",
        )

    detail = {
        "filename": document.filename,
        "subject": document.subject,
        "status": document.status.value,
        "file_path": document.file_path,
        "task_count": len(related_tasks),
        "resource_type": document.resource_type,
        "grade": document.grade,
        "chapter": document.chapter,
        "difficulty": document.difficulty,
        "tags": document.tags,
    }

    try:
        Path(document.file_path).unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete file: {exc}") from exc

    rag_service.clear_document_artifacts(document.id)
    rag_service.purge_document_index(db, document)
    db.delete(document)
    db.commit()
    audit_service.log(
        db,
        actor=current_user,
        action="delete_knowledge_document",
        target_type="knowledge_document",
        target_id=str(document_id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail=detail,
    )
