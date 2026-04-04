from __future__ import annotations

from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.knowledge import DocumentStatus, ImportTask, KnowledgeDocument, ResourceType
from backend.services.rag_service import rag_service
from backend.tasks.celery_app import celery_app

settings = get_settings()
QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}


def _build_completion_message(document: KnowledgeDocument, chunks: list) -> str:
    question_count = 0
    answer_count = 0
    explanation_count = 0
    image_count = 0
    chapters: set[str] = set()
    sections: set[str] = set()

    for chunk in chunks:
        metadata = getattr(chunk, "metadata", {}) or {}
        if metadata.get("chunk_kind") == "question_item":
            question_count += 1
        if metadata.get("answer_text"):
            answer_count += 1
        if metadata.get("explanation_text"):
            explanation_count += 1
        image_count += int(metadata.get("image_count") or 0)

        chapter = str(metadata.get("chapter") or "").strip()
        section = str(metadata.get("section") or "").strip()
        if chapter:
            chapters.add(chapter)
        if section:
            sections.add(section)

    parts = [f"导入完成，共写入 {len(chunks)} 个片段"]
    resource_type = document.resource_type or ResourceType.KNOWLEDGE_NOTE.value
    if question_count:
        parts.append(f"按题目拆分 {question_count} 道题")
        if answer_count:
            parts.append(f"答案 {answer_count} 道")
        if explanation_count:
            parts.append(f"解析 {explanation_count} 道")
    elif resource_type in QUESTION_RESOURCE_TYPES:
        parts.append("未识别到稳定题号，当前按段落切分")
    else:
        parts.append("按段落切分完成")

    if chapters:
        parts.append(f"识别章节 {len(chapters)} 个")
    if sections:
        parts.append(f"识别小节 {len(sections)} 个")
    if image_count:
        parts.append(f"附图 {image_count} 张")
    return "；".join(parts)

def _update_status(
    db: Session,
    task: ImportTask,
    document: KnowledgeDocument,
    *,
    progress: int,
    status: DocumentStatus,
    error: str | None = None,
    celery_task=None,
) -> None:
    task.progress = progress
    task.status = status
    task.error_message = error
    document.status = status
    document.error_message = error
    db.add(task)
    db.add(document)
    db.commit()
    if celery_task is not None:
        state = "PROGRESS" if status == DocumentStatus.PROCESSING else status.value.upper()
        celery_task.update_state(
            state=state,
            meta={
                "task_id": task.id,
                "document_id": document.id,
                "progress": progress,
                "status": status.value,
                "message": error,
            },
        )


def _reload_task_document(db: Session, task_id: int, document_id: int) -> tuple[ImportTask | None, KnowledgeDocument | None]:
    return db.get(ImportTask, task_id), db.get(KnowledgeDocument, document_id)


def _ensure_not_cancelled(db: Session, task_id: int, document_id: int, *, cleanup: bool = False) -> tuple[ImportTask, KnowledgeDocument]:
    task, document = _reload_task_document(db, task_id, document_id)
    if not task or not document:
        raise RuntimeError("导入任务或文档不存在")
    if task.status == DocumentStatus.CANCELLED:
        if cleanup:
            rag_service.purge_document_index(db, document)
        raise RuntimeError("__TASK_CANCELLED__")
    return task, document


def _sync_task_from_result(db: Session, task: ImportTask) -> ImportTask:
    if not task.celery_task_id or task.status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.CANCELLED}:
        return task
    result = AsyncResult(task.celery_task_id, app=celery_app)
    if result.state == "REVOKED":
        task.status = DocumentStatus.CANCELLED
        task.error_message = "任务已被撤销"
    elif result.state == "FAILURE":
        task.status = DocumentStatus.FAILED
        task.error_message = str(result.result)
    elif result.state == "STARTED" and task.status == DocumentStatus.PENDING:
        task.status = DocumentStatus.PROCESSING
        task.error_message = "Worker 已开始执行任务"
    elif result.state == "PROGRESS":
        meta = result.info if isinstance(result.info, dict) else {}
        task.status = DocumentStatus(meta.get("status", DocumentStatus.PROCESSING.value))
        task.progress = int(meta.get("progress", task.progress))
        task.error_message = meta.get("message") or task.error_message
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def run_ingest_pipeline(document_id: int, task_id: int, celery_task=None) -> None:
    db = SessionLocal()
    try:
        task, document = _reload_task_document(db, task_id, document_id)
        if not task or not document:
            return
        if task.status == DocumentStatus.CANCELLED:
            return

        _update_status(
            db,
            task,
            document,
            progress=10,
            status=DocumentStatus.PROCESSING,
            error="开始解析文件",
            celery_task=celery_task,
        )
        _ensure_not_cancelled(db, task_id, document_id)
        extracted = rag_service.extract_content(document.file_path, document.mime_type, document_id=document.id)
        task, document = _ensure_not_cancelled(db, task_id, document_id)
        _update_status(
            db,
            task,
            document,
            progress=25,
            status=DocumentStatus.PROCESSING,
            error=f"文本提取完成，共 {len(extracted.text.strip())} 个字符",
            celery_task=celery_task,
        )
        chunks = rag_service.prepare_document_chunks(document, extracted.text, assets=extracted.assets)
        if not chunks:
            raise RuntimeError("文档未提取到可用文本，无法建立索引")

        def progress_callback(progress: int, message: str) -> None:
            progress_task, progress_document = _ensure_not_cancelled(db, task_id, document_id, cleanup=True)
            _update_status(
                db,
                progress_task,
                progress_document,
                progress=progress,
                status=DocumentStatus.PROCESSING,
                error=message,
                celery_task=celery_task,
            )

        rag_service.ingest_document_chunks(db, document, chunks, progress_callback=progress_callback)
        task, document = _ensure_not_cancelled(db, task_id, document_id)
        _update_status(
            db,
            task,
            document,
            progress=100,
            status=DocumentStatus.COMPLETED,
            error=_build_completion_message(document, chunks),
            celery_task=celery_task,
        )
        db.add(task)
        db.commit()
    except SoftTimeLimitExceeded:
        if "db" in locals():
            task, document = _reload_task_document(db, task_id, document_id)
            if task and document:
                rag_service.clear_document_artifacts(document.id)
                _update_status(
                    db,
                    task,
                    document,
                    progress=100,
                    status=DocumentStatus.FAILED,
                    error="导入超时，请检查文件大小或内容后重试",
                    celery_task=celery_task,
                )
    except RuntimeError as exc:
        if str(exc) == "__TASK_CANCELLED__":
            if "db" in locals():
                task, document = _reload_task_document(db, task_id, document_id)
                if task and document:
                    rag_service.clear_document_artifacts(document.id)
                    _update_status(
                        db,
                        task,
                        document,
                        progress=min(task.progress, 99),
                        status=DocumentStatus.CANCELLED,
                        error="任务已取消，未保留中间索引",
                        celery_task=celery_task,
                    )
            return
        if "db" in locals():
            task, document = _reload_task_document(db, task_id, document_id)
            if task and document:
                rag_service.clear_document_artifacts(document.id)
                _update_status(
                    db,
                    task,
                    document,
                    progress=100,
                    status=DocumentStatus.FAILED,
                    error=str(exc),
                    celery_task=celery_task,
                )
    except Exception as exc:
        if "db" in locals():
            task, document = _reload_task_document(db, task_id, document_id)
            if task and document:
                rag_service.clear_document_artifacts(document.id)
                _update_status(
                    db,
                    task,
                    document,
                    progress=100,
                    status=DocumentStatus.FAILED,
                    error=str(exc),
                    celery_task=celery_task,
                )
    finally:
        db.close()


@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    soft_time_limit=settings.ingest_soft_time_limit_seconds,
    time_limit=settings.ingest_hard_time_limit_seconds,
)
def ingest_document_task(self, document_id: int, task_id: int) -> None:
    run_ingest_pipeline(document_id, task_id, celery_task=self)


def enqueue_ingest_task(document_id: int, task_id: int) -> str | None:
    try:
        result = ingest_document_task.delay(document_id, task_id)
    except Exception:
        return None
    return result.id


def sync_task_state(task: ImportTask, db: Session) -> ImportTask:
    return _sync_task_from_result(db, task)
