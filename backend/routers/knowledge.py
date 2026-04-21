from __future__ import annotations

import mimetypes
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import selectinload

from backend.config import get_settings
from backend.dependencies import CurrentTeacher, CurrentUser, DbSession
from backend.models.knowledge import (
    DifficultyLevel,
    DocumentStatus,
    ImportTask,
    KnowledgeChunk,
    KnowledgeDocument,
    ResourceType,
)
from backend.models.schemas import (
    ImportTaskRead,
    KnowledgeChunkRead,
    KnowledgeDocumentBulkUpdate,
    KnowledgeDocumentRead,
    KnowledgeStructureOptionRead,
    KnowledgeDocumentUpdate,
    KnowledgeQuestionRead,
    KnowledgeQuestionUpdate,
    PaginatedImportTaskRead,
    PaginatedKnowledgeDocumentRead,
    PaginatedKnowledgeQuestionRead,
    StatusSummaryRead,
)
from backend.services.audit_service import audit_service
from backend.services.auto_tag_service import auto_tag_service
from backend.services.document_backup_service import DocumentBackupService
from backend.services.rag_service import UnsupportedQuestionDocxError, rag_service
from backend.tasks.celery_app import celery_app
from backend.tasks.ingest import (
    dispatch_import_task,
    dispatch_next_pdf_task,
    is_pdf_queue_waiting_message,
    sync_task_state,
)
from backend.time_utils import now_beijing

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
settings = get_settings()
ACTIVE_TASK_STATUSES = {DocumentStatus.PENDING, DocumentStatus.PROCESSING}
QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}
METADATA_SUGGESTION_FIELDS = {"chapter", "section", "tag"}
QUESTION_METADATA_SUGGESTION_FIELDS = {"chapter", "tag"}
GENERIC_UPLOAD_MIME_TYPES = {"", "application/octet-stream"}
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
UPLOAD_MIME_COMPATIBILITY = {
    ".pdf": {
        "accepted": {"application/pdf"},
    },
    ".docx": {
        "accepted": {DOCX_MIME_TYPE},
    },
    ".txt": {
        "accepted": {"text/plain"},
    },
    ".md": {
        "accepted": {"text/markdown", "text/x-markdown", "text/plain"},
    },
    ".tex": {
        "accepted": {"text/x-tex", "application/x-tex", "text/plain"},
    },
}


def _resolve_upload_mime_type(file: UploadFile) -> tuple[str, str]:
    suffix = Path(file.filename or "").suffix.lower()
    raw_content_type = (file.content_type or "").strip().lower()
    derived_content_type = (mimetypes.guess_type(file.filename or "")[0] or "").lower()
    compatibility = UPLOAD_MIME_COMPATIBILITY.get(suffix)

    if compatibility is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported MIME type"
        )

    accepted_content_types = {
        content_type
        for content_type in compatibility["accepted"]
        if content_type in settings.upload_mime_type_list
    }

    if raw_content_type in accepted_content_types:
        return raw_content_type, raw_content_type

    if raw_content_type in GENERIC_UPLOAD_MIME_TYPES:
        if derived_content_type in accepted_content_types:
            return raw_content_type, derived_content_type

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported MIME type"
    )


def _resource_type_value(resource_type: ResourceType | str | None) -> str:
    if isinstance(resource_type, ResourceType):
        return resource_type.value
    return str(resource_type or "").strip()


def _requires_question_docx(resource_type: ResourceType | str | None) -> bool:
    return _resource_type_value(resource_type) in QUESTION_RESOURCE_TYPES


def _is_docx_mime_type(content_type: str | None) -> bool:
    return str(content_type or "").strip().lower() == DOCX_MIME_TYPE


def _is_docx_document(document: KnowledgeDocument) -> bool:
    suffix = Path(document.filename or document.file_path or "").suffix.lower()
    return suffix == ".docx" and _is_docx_mime_type(document.mime_type)


def _validate_upload(
    file: UploadFile, *, resource_type: ResourceType | str | None = None
) -> tuple[str, str]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.upload_extension_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported file type"
        )
    raw_content_type, effective_content_type = _resolve_upload_mime_type(file)
    if _requires_question_docx(resource_type) and (
        suffix != ".docx" or not _is_docx_mime_type(effective_content_type)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question resources require DOCX files",
        )
    return raw_content_type, effective_content_type


def _resolve_question_resource_structure(
    db: DbSession,
    *,
    subject: str,
    resource_type: ResourceType | str | None,
    filename: str,
    chapter: str | None,
    section: str | None,
) -> tuple[str | None, str | None]:
    normalized_chapter = _normalize_optional_text(chapter)
    normalized_section = _normalize_optional_text(section)
    if not _requires_question_docx(resource_type):
        return normalized_chapter, normalized_section
    if normalized_chapter:
        return normalized_chapter, normalized_section
    matched = auto_tag_service.match_textbook_structure(db, filename, subject)
    matched_chapter = _normalize_optional_text(matched.get("chapter"))
    matched_section = _normalize_optional_text(matched.get("section"))
    if _resource_type_value(resource_type) == ResourceType.EXERCISE.value and not matched_chapter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chapter is required for exercise DOCX uploads",
        )
    return matched_chapter, normalized_section or matched_section


def _ensure_question_resource_document_is_docx(
    document: KnowledgeDocument, *, target_resource_type: ResourceType | str | None
) -> None:
    if _requires_question_docx(target_resource_type) and not _is_docx_document(document):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question resources require DOCX documents",
        )


def _upload_detail(
    document: KnowledgeDocument, *, raw_mime: str, effective_mime: str
) -> dict:
    detail = _document_detail(document)
    detail["raw_mime"] = raw_mime
    detail["effective_mime"] = effective_mime
    return detail


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_tags(raw_tags: str | list[str] | None) -> list[str]:
    if raw_tags is None:
        return []
    values = (
        raw_tags
        if isinstance(raw_tags, list)
        else raw_tags.replace("，", ",").replace("\n", ",").split(",")
    )
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
    resource_type_value = (
        resource_type.value
        if isinstance(resource_type, ResourceType)
        else str(resource_type).strip()
    )
    if resource_type_value not in {item.value for item in ResourceType}:
        resource_type_value = ResourceType.KNOWLEDGE_NOTE.value

    difficulty_value = (
        difficulty.value
        if isinstance(difficulty, DifficultyLevel)
        else _normalize_optional_text(difficulty)
    )
    if difficulty_value and difficulty_value not in {
        item.value for item in DifficultyLevel
    }:
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
        resource_type=changes.get(
            "resource_type", document.resource_type or ResourceType.KNOWLEDGE_NOTE.value
        ),
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


def _build_status_summary(rows: list[tuple[DocumentStatus, int]]) -> StatusSummaryRead:
    counts = {status: count for status, count in rows}
    return StatusSummaryRead(
        total=sum(counts.values()),
        active=int(counts.get(DocumentStatus.PENDING, 0))
        + int(counts.get(DocumentStatus.PROCESSING, 0)),
        failed=int(counts.get(DocumentStatus.FAILED, 0)),
        completed=int(counts.get(DocumentStatus.COMPLETED, 0)),
        cancelled=int(counts.get(DocumentStatus.CANCELLED, 0)),
    )


def _task_summary(db: DbSession) -> StatusSummaryRead:
    rows = db.execute(
        select(ImportTask.status, func.count(ImportTask.id)).group_by(ImportTask.status)
    ).all()
    return _build_status_summary(rows)


def _document_summary(db: DbSession) -> StatusSummaryRead:
    rows = db.execute(
        select(KnowledgeDocument.status, func.count(KnowledgeDocument.id)).group_by(
            KnowledgeDocument.status
        )
    ).all()
    return _build_status_summary(rows)


def _document_read(
    document: KnowledgeDocument, *, has_active_task: bool = False
) -> KnowledgeDocumentRead:
    return KnowledgeDocumentRead(
        id=document.id,
        subject=document.subject,
        filename=document.filename,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        resource_type=document.resource_type,
        grade=document.grade,
        chapter=document.chapter,
        section=document.section,
        difficulty=document.difficulty,
        tags=document.tags,
        status=document.status,
        has_active_task=has_active_task,
        error_message=document.error_message,
        created_at=document.created_at,
    )


def _rank_suggestions(
    values: list[str], *, query: str | None, limit: int
) -> list[str]:
    normalized_query = (query or "").strip().lower()
    deduped: dict[str, str] = {}
    for value in values:
        normalized = str(value).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if normalized_query and normalized_query not in key:
            continue
        deduped.setdefault(key, normalized)
    return sorted(
        deduped.values(),
        key=lambda item: (
            not item.lower().startswith(normalized_query),
            item.lower(),
        ),
    )[:limit]


def _tags_match_query(tags: list[str] | None, query: str | None) -> bool:
    normalized_query = (query or "").strip().lower()
    if not normalized_query:
        return True
    return any(normalized_query in str(tag).strip().lower() for tag in (tags or []))


def _metadata_suggestions(
    db: DbSession,
    *,
    field: str,
    query: str | None,
    subject: str | None,
    limit: int,
) -> list[str]:
    if field not in METADATA_SUGGESTION_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported metadata suggestion field",
        )
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid suggestion limit",
        )

    if field == "tag":
        stmt = select(KnowledgeDocument.tags_json)
        if subject:
            stmt = stmt.where(KnowledgeDocument.subject == subject)
        rows = db.scalars(stmt).all()
        values = [
            str(tag).strip()
            for tag_list in rows
            for tag in (tag_list or [])
            if str(tag).strip()
        ]
        return _rank_suggestions(values, query=query, limit=limit)

    column = (
        KnowledgeDocument.chapter
        if field == "chapter"
        else KnowledgeDocument.section
    )
    stmt = select(column).where(column.is_not(None))
    if subject:
        stmt = stmt.where(KnowledgeDocument.subject == subject)
    if query:
        stmt = stmt.where(column.ilike(f"%{query.strip()}%"))
    values = [item for item in db.scalars(stmt.distinct()).all() if item]
    return _rank_suggestions(values, query=query, limit=limit)


def _question_row_matches(row: KnowledgeChunk) -> bool:
    document = row.document
    if not document:
        return False
    if (document.resource_type or ResourceType.KNOWLEDGE_NOTE.value) not in QUESTION_RESOURCE_TYPES:
        return False
    metadata = row.metadata_json or {}
    return str(metadata.get("chunk_kind") or "").strip() == "question_item"


def _metadata_or_document_value(
    metadata: dict,
    document: KnowledgeDocument | None,
    key: str,
) -> str | int | list[str] | None:
    if key in metadata:
        return metadata.get(key)
    if document is None:
        return None
    if key == "tags":
        return list(document.tags)
    return getattr(document, key, None)


def _question_text_value(row: KnowledgeChunk) -> str:
    metadata = row.metadata_json or {}
    return str(metadata.get("question_text") or row.content or "").strip()


def _question_read(row: KnowledgeChunk) -> KnowledgeQuestionRead:
    metadata = row.metadata_json or {}
    document = row.document
    tags_value = _metadata_or_document_value(metadata, document, "tags") or []
    asset_refs = list(metadata.get("asset_refs") or [])
    return KnowledgeQuestionRead(
        id=row.id,
        document_id=row.document_id,
        document_filename=document.filename if document else None,
        subject=row.subject,
        resource_type=str(
            metadata.get("resource_type")
            or (document.resource_type if document else "")
            or ""
        ),
        grade=_metadata_or_document_value(metadata, document, "grade"),
        chapter=_metadata_or_document_value(metadata, document, "chapter"),
        section=_metadata_or_document_value(metadata, document, "section"),
        difficulty=_metadata_or_document_value(metadata, document, "difficulty"),
        tags=list(tags_value) if isinstance(tags_value, list) else [],
        question_number=str(metadata.get("question_number") or "").strip() or None,
        question_text=_question_text_value(row),
        is_disabled=bool(row.is_disabled),
        contains_images=bool(metadata.get("contains_images")),
        image_count=int(metadata.get("image_count") or 0),
        assets=asset_refs,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _question_matches_filters(
    row: KnowledgeChunk,
    *,
    difficulty: str | None,
    chapter: str | None,
    tag: str | None,
    disabled: bool | None,
    keyword: str | None,
) -> bool:
    if not _question_row_matches(row):
        return False
    metadata = row.metadata_json or {}
    document = row.document

    if disabled is not None and bool(row.is_disabled) != disabled:
        return False

    effective_difficulty = str(
        _metadata_or_document_value(metadata, document, "difficulty") or ""
    ).strip()
    if difficulty and effective_difficulty != difficulty:
        return False

    effective_chapter = str(
        _metadata_or_document_value(metadata, document, "chapter") or ""
    ).strip()
    if chapter and chapter.strip().lower() not in effective_chapter.lower():
        return False

    effective_tags = _metadata_or_document_value(metadata, document, "tags") or []
    if tag and not _tags_match_query(
        list(effective_tags) if isinstance(effective_tags, list) else [], tag
    ):
        return False

    if keyword:
        normalized_keyword = keyword.strip().lower()
        haystacks = [
            str(metadata.get("question_number") or "").strip().lower(),
            _question_text_value(row).lower(),
        ]
        if not any(normalized_keyword in haystack for haystack in haystacks if haystack):
            return False

    return True


def _question_rows(
    db: DbSession,
    *,
    subject: str | None = None,
    resource_type: str | None = None,
) -> list[KnowledgeChunk]:
    stmt = (
        select(KnowledgeChunk)
        .join(KnowledgeChunk.document)
        .options(selectinload(KnowledgeChunk.document))
        .where(KnowledgeDocument.resource_type.in_(tuple(QUESTION_RESOURCE_TYPES)))
        .order_by(KnowledgeChunk.updated_at.desc(), KnowledgeChunk.id.desc())
    )
    if subject:
        stmt = stmt.where(KnowledgeChunk.subject == subject)
    if resource_type:
        stmt = stmt.where(KnowledgeDocument.resource_type == resource_type)
    return db.scalars(stmt).all()


def _question_metadata_suggestions(
    db: DbSession,
    *,
    field: str,
    query: str | None,
    subject: str | None,
    resource_type: str | None,
    disabled: bool | None,
    limit: int,
) -> list[str]:
    if field not in QUESTION_METADATA_SUGGESTION_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported question metadata suggestion field",
        )
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid suggestion limit",
        )

    values: list[str] = []
    for row in _question_rows(
        db,
        subject=subject,
        resource_type=resource_type,
    ):
        if not _question_matches_filters(
            row,
            difficulty=None,
            chapter=None,
            tag=None,
            disabled=disabled,
            keyword=None,
        ):
            continue
        metadata = row.metadata_json or {}
        document = row.document
        if field == "chapter":
            chapter_value = _metadata_or_document_value(metadata, document, "chapter")
            if chapter_value:
                values.append(str(chapter_value).strip())
            continue
        tags_value = _metadata_or_document_value(metadata, document, "tags") or []
        if isinstance(tags_value, list):
            values.extend(
                str(item).strip() for item in tags_value if str(item).strip()
            )
    return _rank_suggestions(values, query=query, limit=limit)


def _chunk_read(row: KnowledgeChunk) -> KnowledgeChunkRead:
    metadata = row.metadata_json or {}
    document = row.document
    return KnowledgeChunkRead(
        id=row.id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        subject=row.subject,
        resource_type=str(
            metadata.get("resource_type")
            or (document.resource_type if document else "")
            or ""
        ),
        grade=metadata.get("grade") or (document.grade if document else None),
        chapter=_metadata_or_document_value(metadata, document, "chapter"),
        section=_metadata_or_document_value(metadata, document, "section"),
        difficulty=_metadata_or_document_value(metadata, document, "difficulty"),
        tags=list(_metadata_or_document_value(metadata, document, "tags") or []),
        chunk_kind=metadata.get("chunk_kind"),
        question_number=metadata.get("question_number"),
        question_text=metadata.get("question_text"),
        answer_text=metadata.get("answer_text"),
        explanation_text=metadata.get("explanation_text"),
        is_disabled=bool(row.is_disabled),
        contains_images=bool(metadata.get("contains_images")),
        image_count=int(metadata.get("image_count") or 0),
        assets=list(metadata.get("asset_refs") or []),
    )


def _sync_document_state(
    document: KnowledgeDocument, db: DbSession
) -> KnowledgeDocument:
    latest_task = db.scalar(
        select(ImportTask)
        .where(ImportTask.document_id == document.id)
        .order_by(ImportTask.updated_at.desc())
        .limit(1)
    )
    if latest_task:
        latest_task = sync_task_state(latest_task, db)
        if (
            document.status != latest_task.status
            or document.error_message != latest_task.error_message
        ):
            document.status = latest_task.status
            document.error_message = latest_task.error_message
            db.add(document)
            db.commit()
            db.refresh(document)
        return document

    if document.status in ACTIVE_TASK_STATUSES:
        document.status = DocumentStatus.FAILED
        document.error_message = (
            document.error_message or "未找到关联导入任务，可删除后重新上传"
        )
        db.add(document)
        db.commit()
        db.refresh(document)
    return document


@router.get(
    "/documents",
    response_model=list[KnowledgeDocumentRead] | PaginatedKnowledgeDocumentRead,
)
def list_documents(
    db: DbSession,
    current_user: CurrentTeacher,
    page: int | None = None,
    page_size: int | None = None,
    subject: str | None = None,
    status_filter: DocumentStatus | None = None,
    resource_type: str | None = None,
    grade: int | None = None,
    difficulty: str | None = None,
    chapter: str | None = None,
    section: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
) -> list[KnowledgeDocumentRead] | PaginatedKnowledgeDocumentRead:
    dispatch_next_pdf_task(db)
    is_paginated = any(
        value is not None and value != ""
        for value in [
            page,
            page_size,
            subject,
            status_filter,
            resource_type,
            grade,
            difficulty,
            chapter,
            section,
            tag,
            keyword,
        ]
    )
    if not is_paginated:
        documents = db.scalars(
            select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc())
        ).all()
        active_ids = {
            item
            for item in db.scalars(
                select(ImportTask.document_id)
                .where(ImportTask.status.in_(ACTIVE_TASK_STATUSES))
                .distinct()
            ).all()
        }
        return [
            _document_read(
                _sync_document_state(item, db), has_active_task=item.id in active_ids
            )
            for item in documents
        ]

    page = page or 1
    page_size = page_size or 20
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination parameters",
        )

    query = select(KnowledgeDocument)
    count_query = select(func.count(KnowledgeDocument.id))

    if subject:
        query = query.where(KnowledgeDocument.subject == subject)
        count_query = count_query.where(KnowledgeDocument.subject == subject)
    if status_filter:
        query = query.where(KnowledgeDocument.status == status_filter)
        count_query = count_query.where(KnowledgeDocument.status == status_filter)
    if resource_type:
        query = query.where(KnowledgeDocument.resource_type == resource_type)
        count_query = count_query.where(KnowledgeDocument.resource_type == resource_type)
    if grade is not None:
        query = query.where(KnowledgeDocument.grade == grade)
        count_query = count_query.where(KnowledgeDocument.grade == grade)
    if difficulty:
        query = query.where(KnowledgeDocument.difficulty == difficulty)
        count_query = count_query.where(KnowledgeDocument.difficulty == difficulty)
    if chapter:
        query = query.where(KnowledgeDocument.chapter.ilike(f"%{chapter.strip()}%"))
        count_query = count_query.where(
            KnowledgeDocument.chapter.ilike(f"%{chapter.strip()}%")
        )
    if section:
        query = query.where(KnowledgeDocument.section.ilike(f"%{section.strip()}%"))
        count_query = count_query.where(
            KnowledgeDocument.section.ilike(f"%{section.strip()}%")
        )
    if keyword:
        like_keyword = f"%{keyword.strip()}%"
        keyword_clause = or_(
            KnowledgeDocument.filename.ilike(like_keyword),
            KnowledgeDocument.chapter.ilike(like_keyword),
            KnowledgeDocument.section.ilike(like_keyword),
            KnowledgeDocument.error_message.ilike(like_keyword),
            cast(KnowledgeDocument.tags_json, String).ilike(like_keyword),
        )
        query = query.where(keyword_clause)
        count_query = count_query.where(keyword_clause)

    if tag:
        matching_rows = db.execute(
            query.with_only_columns(
                KnowledgeDocument.id, KnowledgeDocument.tags_json, maintain_column_froms=True
            ).order_by(KnowledgeDocument.created_at.desc())
        ).all()
        matching_ids = [
            document_id
            for document_id, tags_json in matching_rows
            if _tags_match_query(tags_json, tag)
        ]
        total = len(matching_ids)
        page_ids = matching_ids[(page - 1) * page_size : page * page_size]
        if not page_ids:
            synced_rows: list[KnowledgeDocument] = []
        else:
            row_map = {
                row.id: row
                for row in db.scalars(
                    select(KnowledgeDocument)
                    .where(KnowledgeDocument.id.in_(page_ids))
                    .order_by(KnowledgeDocument.created_at.desc())
                ).all()
            }
            synced_rows = [_sync_document_state(row_map[item], db) for item in page_ids if item in row_map]
    else:
        total = int(db.scalar(count_query) or 0)
        rows = db.scalars(
            query.order_by(KnowledgeDocument.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        synced_rows = [_sync_document_state(item, db) for item in rows]
    active_ids = set()
    if synced_rows:
        active_ids = {
            item
            for item in db.scalars(
                select(ImportTask.document_id)
                .where(
                    ImportTask.document_id.in_([row.id for row in synced_rows]),
                    ImportTask.status.in_(ACTIVE_TASK_STATUSES),
                )
                .distinct()
            ).all()
        }
    return PaginatedKnowledgeDocumentRead(
        items=[
            _document_read(item, has_active_task=item.id in active_ids)
            for item in synced_rows
        ],
        page=page,
        page_size=page_size,
        total=total,
        summary=_document_summary(db),
    )


@router.get("/metadata-suggestions", response_model=list[str])
def list_metadata_suggestions(
    field: str,
    db: DbSession,
    current_user: CurrentTeacher,
    query: str | None = None,
    subject: str | None = None,
    limit: int = 10,
) -> list[str]:
    return _metadata_suggestions(
        db,
        field=field,
        query=query,
        subject=subject,
        limit=limit,
    )


@router.get("/textbook-structure-options", response_model=list[KnowledgeStructureOptionRead])
def list_textbook_structure_options(
    subject: str,
    db: DbSession,
    current_user: CurrentTeacher,
) -> list[KnowledgeStructureOptionRead]:
    del current_user
    return [
        KnowledgeStructureOptionRead.model_validate(item)
        for item in auto_tag_service.list_textbook_structure_options(db, subject)
    ]


@router.get("/questions", response_model=PaginatedKnowledgeQuestionRead)
def list_questions(
    db: DbSession,
    current_user: CurrentTeacher,
    page: int = 1,
    page_size: int = 20,
    subject: str | None = None,
    resource_type: str | None = None,
    difficulty: str | None = None,
    chapter: str | None = None,
    tag: str | None = None,
    disabled: bool | None = None,
    keyword: str | None = None,
) -> PaginatedKnowledgeQuestionRead:
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination parameters",
        )

    rows = [
        row
        for row in _question_rows(
            db,
            subject=subject,
            resource_type=resource_type,
        )
        if _question_matches_filters(
            row,
            difficulty=difficulty,
            chapter=chapter,
            tag=tag,
            disabled=disabled,
            keyword=keyword,
        )
    ]
    total = len(rows)
    page_rows = rows[(page - 1) * page_size : page * page_size]
    return PaginatedKnowledgeQuestionRead(
        items=[_question_read(row) for row in page_rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/questions/metadata-suggestions", response_model=list[str])
def list_question_metadata_suggestions(
    field: str,
    db: DbSession,
    current_user: CurrentTeacher,
    query: str | None = None,
    subject: str | None = None,
    resource_type: str | None = None,
    disabled: bool | None = None,
    limit: int = 10,
) -> list[str]:
    return _question_metadata_suggestions(
        db,
        field=field,
        query=query,
        subject=subject,
        resource_type=resource_type,
        disabled=disabled,
        limit=limit,
    )


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentRead)
def get_document(
    document_id: int, db: DbSession, current_user: CurrentTeacher
) -> KnowledgeDocumentRead:
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    document = _sync_document_state(document, db)
    has_active_task = bool(
        db.scalar(
            select(ImportTask.id)
            .where(
                ImportTask.document_id == document_id,
                ImportTask.status.in_(ACTIVE_TASK_STATUSES),
            )
            .limit(1)
        )
    )
    return _document_read(document, has_active_task=has_active_task)


@router.post(
    "/documents/{document_id}/reingest",
    response_model=ImportTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def reingest_document(
    document_id: int,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> ImportTaskRead:
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    related_tasks = [sync_task_state(task, db) for task in list(document.tasks)]
    active_tasks = [
        task for task in related_tasks if task.status in ACTIVE_TASK_STATUSES
    ]
    if active_tasks:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document has active import task",
        )

    source_path = Path(document.file_path)
    if not source_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source file not found",
        )

    document.status = DocumentStatus.PENDING
    document.error_message = None
    db.add(document)
    db.commit()
    db.refresh(document)

    task = ImportTask(
        document_id=document.id,
        status=DocumentStatus.PENDING,
        progress=0,
        error_message="任务已创建，等待重新切片",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    dispatch_import_task(db, document, task)
    db.refresh(task)
    db.refresh(document)

    audit_service.log(
        db,
        actor=current_user,
        action="reingest_knowledge_document",
        target_type="knowledge_document",
        target_id=str(document.id),
        result="accepted",
        ip_address=request.client.host if request.client else None,
        detail={
            "filename": document.filename,
            "subject": document.subject,
            "resource_type": document.resource_type,
            "task_id": task.id,
        },
    )
    return ImportTaskRead.model_validate(task)


def _rebuild_question_metadata(
    *,
    document: KnowledgeDocument,
    row: KnowledgeChunk,
    chapter: str | None,
    section: str | None,
    difficulty: str | None,
    tags: list[str],
) -> dict:
    metadata = dict(row.metadata_json or {})
    metadata.update(
        {
            "document_id": document.id,
            "filename": document.filename,
            "subject": document.subject,
            "resource_type": document.resource_type
            or ResourceType.KNOWLEDGE_NOTE.value,
            "grade": document.grade,
            "chapter": chapter,
            "section": section,
            "difficulty": difficulty,
            "tags": tags,
        }
    )
    for key in (
        "chapter_key",
        "section_key",
        "structure_path",
        "retrieval_metadata",
        "diagnostic_metadata",
        "ingestion_metadata",
    ):
        metadata.pop(key, None)
    return rag_service._apply_metadata_layers(metadata)


def _question_row_or_404(chunk_id: int, db: DbSession) -> KnowledgeChunk:
    row = db.scalar(
        select(KnowledgeChunk)
        .options(selectinload(KnowledgeChunk.document))
        .where(KnowledgeChunk.id == chunk_id)
        .limit(1)
    )
    if not row or not _question_row_matches(row):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found",
        )
    return row


@router.put("/questions/{chunk_id}", response_model=KnowledgeQuestionRead)
def update_question(
    chunk_id: int,
    payload: KnowledgeQuestionUpdate,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> KnowledgeQuestionRead:
    row = _question_row_or_404(chunk_id, db)
    changes = payload.model_dump(exclude_unset=True, mode="json")
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No question metadata changes provided",
        )

    metadata = row.metadata_json or {}
    document = row.document
    assert document is not None
    chapter = (
        _normalize_optional_text(changes["chapter"])
        if "chapter" in changes
        else _metadata_or_document_value(metadata, document, "chapter")
    )
    section = (
        _normalize_optional_text(changes["section"])
        if "section" in changes
        else _metadata_or_document_value(metadata, document, "section")
    )
    difficulty = (
        _normalize_optional_text(changes["difficulty"])
        if "difficulty" in changes
        else _metadata_or_document_value(metadata, document, "difficulty")
    )
    tags = (
        _normalize_tags(changes["tags"])
        if "tags" in changes
        else list(_metadata_or_document_value(metadata, document, "tags") or [])
    )
    row.metadata_json = _rebuild_question_metadata(
        document=document,
        row=row,
        chapter=chapter,
        section=section,
        difficulty=difficulty,
        tags=tags,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    rag_service.vector_store.upsert_chunks(document.subject, [row])
    audit_service.log(
        db,
        actor=current_user,
        action="update_knowledge_question",
        target_type="knowledge_chunk",
        target_id=str(row.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={
            "document_id": row.document_id,
            "document_filename": document.filename,
            "updated_fields": sorted(changes.keys()),
            "chapter": row.metadata_json.get("chapter"),
            "section": row.metadata_json.get("section"),
            "difficulty": row.metadata_json.get("difficulty"),
            "tags": row.metadata_json.get("tags") or [],
        },
    )
    return _question_read(row)


@router.post("/questions/{chunk_id}/disable", response_model=KnowledgeQuestionRead)
def disable_question(
    chunk_id: int,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> KnowledgeQuestionRead:
    row = _question_row_or_404(chunk_id, db)
    row.is_disabled = True
    db.add(row)
    db.commit()
    db.refresh(row)
    document = row.document
    audit_service.log(
        db,
        actor=current_user,
        action="disable_knowledge_question",
        target_type="knowledge_chunk",
        target_id=str(row.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={
            "document_id": row.document_id,
            "document_filename": document.filename if document else None,
            "is_disabled": True,
        },
    )
    return _question_read(row)


@router.post("/questions/{chunk_id}/restore", response_model=KnowledgeQuestionRead)
def restore_question(
    chunk_id: int,
    db: DbSession,
    current_user: CurrentTeacher,
    request: Request,
) -> KnowledgeQuestionRead:
    row = _question_row_or_404(chunk_id, db)
    row.is_disabled = False
    db.add(row)
    db.commit()
    db.refresh(row)
    document = row.document
    audit_service.log(
        db,
        actor=current_user,
        action="restore_knowledge_question",
        target_type="knowledge_chunk",
        target_id=str(row.id),
        result="success",
        ip_address=request.client.host if request.client else None,
        detail={
            "document_id": row.document_id,
            "document_filename": document.filename if document else None,
            "is_disabled": False,
        },
    )
    return _question_read(row)


@router.get("/documents/{document_id}/chunks", response_model=list[KnowledgeChunkRead])
def list_document_chunks(
    document_id: int, db: DbSession, current_user: CurrentTeacher
) -> list[KnowledgeChunkRead]:
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    rows = db.scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == document_id)
        .order_by(KnowledgeChunk.chunk_index.asc())
    ).all()
    return [_chunk_read(row) for row in rows]


@router.get("/documents/{document_id}/assets/{asset_name}")
def get_document_asset(
    document_id: int, asset_name: str, db: DbSession, current_user: CurrentUser
):
    document = db.get(KnowledgeDocument, document_id)
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    asset_dir = rag_service.document_asset_dir(document_id).resolve()
    asset_path = (asset_dir / asset_name).resolve()
    if asset_dir not in asset_path.parents or not asset_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found"
        )
    media_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
    return FileResponse(asset_path, media_type=media_type, filename=asset_path.name)


@router.post(
    "/upload", response_model=ImportTaskRead, status_code=status.HTTP_202_ACCEPTED
)
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
    raw_content_type, content_type = _validate_upload(file, resource_type=resource_type)
    resolved_chapter, resolved_section = _resolve_question_resource_structure(
        db,
        subject=subject,
        resource_type=resource_type,
        filename=file.filename or "",
        chapter=chapter,
        section=section,
    )
    content = await file.read()
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File too large"
        )

    saved_name = f"{now_beijing().strftime('%Y%m%d%H%M%S')}_{uuid4().hex}{Path(file.filename or '').suffix.lower()}"
    target_path = Path(settings.upload_path) / saved_name
    target_path.write_bytes(content)
    if _requires_question_docx(resource_type):
        try:
            rag_service.ensure_question_resource_docx_supported(str(target_path))
        except UnsupportedQuestionDocxError as exc:
            target_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    document = KnowledgeDocument(
        subject=subject,
        filename=file.filename or saved_name,
        file_path=str(target_path),
        mime_type=content_type,
        size_bytes=len(content),
        status=DocumentStatus.PENDING,
        created_by=current_user.id,
    )
    _apply_document_metadata(
        document,
        resource_type=resource_type,
        grade=grade,
        chapter=resolved_chapter,
        section=resolved_section,
        difficulty=difficulty,
        tags=tags,
    )
    if resource_type == ResourceType.KNOWLEDGE_NOTE.value or (
        isinstance(resource_type, str)
        and resource_type == ResourceType.KNOWLEDGE_NOTE.value
    ):
        document.tags_json = auto_tag_service.auto_tag(
            db,
            filename=file.filename or saved_name,
            subject=subject,
            existing_tags=document.tags,
        )
    db.add(document)
    db.commit()
    db.refresh(document)
    try:
        backup_path = DocumentBackupService(settings).persist_uploaded_file(
            target_path, document
        )
    except OSError as exc:
        db.delete(document)
        db.commit()
        target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist document backup: {exc}",
        ) from exc
    document.file_path = str(backup_path)
    db.add(document)
    db.commit()
    db.refresh(document)

    task = ImportTask(
        document_id=document.id,
        status=DocumentStatus.PENDING,
        progress=0,
        error_message="任务已创建，等待分配 worker",
    )
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
        detail=_upload_detail(
            document, raw_mime=raw_content_type, effective_mime=content_type
        ),
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
    documents = db.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids))
    ).all()
    document_map = {document.id: document for document in documents}
    missing_ids = [
        document_id for document_id in document_ids if document_id not in document_map
    ]
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

    changes = payload.model_dump(
        exclude={"document_ids"}, exclude_unset=True, mode="json"
    )
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No metadata changes provided",
        )

    for document in ordered_documents:
        _ensure_question_resource_document_is_docx(
            document,
            target_resource_type=changes.get("resource_type", document.resource_type),
        )
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
    return [
        KnowledgeDocumentRead.model_validate(document) for document in updated_documents
    ]


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    related_tasks = [sync_task_state(task, db) for task in list(document.tasks)]
    if any(task.status in ACTIVE_TASK_STATUSES for task in related_tasks):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document has active import task",
        )

    _ensure_question_resource_document_is_docx(
        document,
        target_resource_type=payload.resource_type,
    )
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


@router.get("/tasks", response_model=list[ImportTaskRead] | PaginatedImportTaskRead)
def list_tasks(
    limit: int | None = None,
    db: DbSession = None,
    current_user: CurrentTeacher = None,
    page: int | None = None,
    page_size: int | None = None,
    status_filter: DocumentStatus | None = None,
) -> list[ImportTaskRead] | PaginatedImportTaskRead:
    dispatch_next_pdf_task(db)
    is_paginated = page is not None or page_size is not None or status_filter is not None
    query = select(ImportTask)
    count_query = select(func.count(ImportTask.id))
    if status_filter is not None:
        query = query.where(ImportTask.status == status_filter)
        count_query = count_query.where(ImportTask.status == status_filter)
    query = query.order_by(ImportTask.updated_at.desc())

    if not is_paginated:
        if limit is not None:
            query = query.limit(limit)
        tasks = db.scalars(query).all()
        return [ImportTaskRead.model_validate(sync_task_state(task, db)) for task in tasks]

    page = page or 1
    page_size = page_size or 10
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination parameters",
        )
    total = int(db.scalar(count_query) or 0)
    tasks = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return PaginatedImportTaskRead(
        items=[ImportTaskRead.model_validate(sync_task_state(task, db)) for task in tasks],
        page=page,
        page_size=page_size,
        total=total,
        summary=_task_summary(db),
    )


@router.get("/tasks/{task_id}", response_model=ImportTaskRead)
def get_task(
    task_id: int, db: DbSession, current_user: CurrentTeacher
) -> ImportTaskRead:
    dispatch_next_pdf_task(db)
    task = db.get(ImportTask, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    task = sync_task_state(task, db)
    return ImportTaskRead.model_validate(task)


@router.post("/tasks/{task_id}/cancel", response_model=ImportTaskRead)
def cancel_task(
    task_id: int, db: DbSession, current_user: CurrentTeacher
) -> ImportTaskRead:
    task = db.get(ImportTask, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    if task.status in {
        DocumentStatus.COMPLETED,
        DocumentStatus.FAILED,
        DocumentStatus.CANCELLED,
    }:
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    task = sync_task_state(task, db)
    if task.status in ACTIVE_TASK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Active task cannot be deleted"
        )

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    related_tasks = [sync_task_state(task, db) for task in list(document.tasks)]
    active_tasks = [
        task for task in related_tasks if task.status in ACTIVE_TASK_STATUSES
    ]
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete file: {exc}",
        ) from exc

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
