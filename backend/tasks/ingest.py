from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
from pathlib import Path
import threading

from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.knowledge import DocumentStatus, ImportTask, KnowledgeDocument, ResourceType
from backend.services.mineru_service import (
    GPUProofFailedError,
    MineruGpuPreflightError,
    MineruGpuRuntimeError,
    MineruStartupError,
    MineruTransientIOError,
)
from backend.services.document_backup_service import DocumentBackupService
from backend.services.pdf_parse_types import PDFParseResult
from backend.services.rag_service import ExtractionResult, rag_service
from backend.tasks.celery_app import celery_app

settings = get_settings()
QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}
RETRIABLE_INGEST_EXCEPTIONS = (MineruStartupError, MineruTransientIOError)
PDF_QUEUE_WAITING_MESSAGE = "排队中，等待前一条任务处理完成"
LEGACY_PDF_QUEUE_WAITING_MESSAGE = "PDF 导入排队中，等待前序任务完成"
TASK_CREATED_MESSAGE = "任务已创建，等待分配 worker"
LOCAL_TASK_CREATED_MESSAGE = "任务已创建，等待本地处理"
STALE_PENDING_TASK_SECONDS = max(30, settings.ingest_poll_interval_seconds * 10)


def _build_extraction_message(extracted: ExtractionResult) -> str:
    return f"文本提取完成，共 {len(extracted.text.strip())} 个字符"


def is_pdf_queue_waiting_message(message: str | None) -> bool:
    normalized = str(message or "").strip()
    return normalized in {PDF_QUEUE_WAITING_MESSAGE, LEGACY_PDF_QUEUE_WAITING_MESSAGE}


def _build_completion_message(document: KnowledgeDocument, chunks: list, parsed_pdf: PDFParseResult | None = None) -> str:
    question_count = 0
    answer_count = 0
    explanation_count = 0
    image_count = 0
    has_grouped_question_identifier = False
    chapters: set[str] = set()
    sections: set[str] = set()

    for chunk in chunks:
        metadata = getattr(chunk, "metadata", {}) or {}
        if metadata.get("chunk_kind") == "question_item":
            question_count += 1
            question_number = str(metadata.get("question_number") or "").strip()
            if "-" in question_number:
                has_grouped_question_identifier = True
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
        parts.append(
            f"按题目/题块拆分 {question_count} 个"
            if has_grouped_question_identifier
            else f"按题目拆分 {question_count} 道题"
        )
        if answer_count:
            parts.append(f"答案 {answer_count} 个" if has_grouped_question_identifier else f"答案 {answer_count} 道")
        if explanation_count:
            parts.append(f"解析 {explanation_count} 个" if has_grouped_question_identifier else f"解析 {explanation_count} 道")
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


def _gpu_required_for_ingest(parsed_pdf: PDFParseResult | None) -> bool:
    if parsed_pdf is None:
        return False
    service_settings = getattr(rag_service, "settings", settings)
    return (
        getattr(service_settings, "pdf_parser_backend", None) == "mineru"
        and getattr(service_settings, "mineru_device", None) == "cuda"
    )


def _read_runtime_artifact(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GPUProofFailedError("MinerU GPU 运行凭证缺失") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GPUProofFailedError("MinerU GPU 运行凭证不可读") from exc


def _ensure_gpu_requirement_satisfied(extracted: ExtractionResult) -> None:
    parsed_pdf = extracted.parsed_pdf
    if not _gpu_required_for_ingest(parsed_pdf):
        return

    provenance = parsed_pdf.parser_provenance or {}
    effective_device = provenance.get("effective_device") or provenance.get("device")
    if effective_device != "cuda":
        raise GPUProofFailedError("MinerU 解析结果显示未实际使用 GPU")

    service_settings = getattr(rag_service, "settings", settings)
    if not getattr(service_settings, "mineru_require_gpu_proof", False):
        return

    runtime_artifact = str(provenance.get("runtime_artifact") or "").strip()
    if not runtime_artifact:
        raise GPUProofFailedError("MinerU GPU 运行凭证缺失")

    artifact = _read_runtime_artifact(runtime_artifact)
    artifact_device = artifact.get("effective_device") or artifact.get("selected_device") or artifact.get("device")
    if artifact_device != "cuda":
        raise GPUProofFailedError("MinerU GPU 运行凭证显示解析未使用 GPU")
    if not artifact.get("gpu_proof_passed"):
        raise GPUProofFailedError("MinerU GPU 运行凭证未通过校验")


def _build_gpu_failure_message(exc: Exception) -> str:
    if isinstance(exc, MineruGpuPreflightError):
        prefix = "PDF 解析要求使用 GPU，但当前 GPU 环境未就绪，请检查 CUDA、驱动、MinerU 安装和 nvidia-smi 后重试"
    elif isinstance(exc, MineruGpuRuntimeError):
        prefix = "PDF 解析要求使用 GPU，但 MinerU 运行时未能成功使用 CUDA，请检查 GPU 状态后重试"
    else:
        prefix = "PDF 解析要求使用 GPU，但本次任务未保留有效 GPU 运行凭证，请检查 mineru-runtime.json 和 GPU 环境后重试"
    detail = str(exc).strip()
    if detail:
        return f"{prefix}（详情：{detail}）"
    return prefix


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


def _is_pdf_document(document: KnowledgeDocument | None) -> bool:
    if document is None:
        return False
    mime_type = str(document.mime_type or "").lower()
    suffix = Path(document.file_path or document.filename or "").suffix.lower()
    return mime_type == "application/pdf" or suffix == ".pdf"


def _normalize_timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


@contextmanager
def _pdf_dispatch_lock() -> None:
    lock_path = Path(settings.task_artifact_path) / "pdf-import-dispatch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _has_active_pdf_task(db: Session) -> bool:
    active_task = db.scalar(
        select(ImportTask.id)
        .join(KnowledgeDocument, KnowledgeDocument.id == ImportTask.document_id)
        .where(
            KnowledgeDocument.mime_type == "application/pdf",
            or_(
                ImportTask.status == DocumentStatus.PROCESSING,
                (ImportTask.status == DocumentStatus.PENDING) & (ImportTask.error_message != PDF_QUEUE_WAITING_MESSAGE),
            ),
        )
        .limit(1)
    )
    return active_task is not None


def _next_pending_pdf_task(db: Session) -> ImportTask | None:
    return db.scalar(
        select(ImportTask)
        .join(KnowledgeDocument, KnowledgeDocument.id == ImportTask.document_id)
        .where(
            KnowledgeDocument.mime_type == "application/pdf",
            ImportTask.status == DocumentStatus.PENDING,
            or_(ImportTask.error_message == PDF_QUEUE_WAITING_MESSAGE, ImportTask.error_message.is_(None)),
        )
        .order_by(ImportTask.created_at.asc(), ImportTask.id.asc())
        .limit(1)
    )


def _start_task_execution(
    db: Session,
    document: KnowledgeDocument,
    task: ImportTask,
    *,
    background_tasks=None,
) -> str | None:
    celery_task_id = enqueue_ingest_task(document.id, task.id)
    if celery_task_id:
        task.celery_task_id = celery_task_id
        task.error_message = TASK_CREATED_MESSAGE
        db.add(task)
        db.commit()
        db.refresh(task)
        return celery_task_id

    task.celery_task_id = None
    task.error_message = LOCAL_TASK_CREATED_MESSAGE
    db.add(task)
    db.commit()
    db.refresh(task)

    if background_tasks is not None:
        background_tasks.add_task(run_ingest_pipeline, document.id, task.id)
    else:
        threading.Thread(target=run_ingest_pipeline, args=(document.id, task.id), daemon=True).start()
    return None


def dispatch_import_task(
    db: Session,
    document: KnowledgeDocument,
    task: ImportTask,
    *,
    background_tasks=None,
) -> bool:
    if not _is_pdf_document(document):
        _start_task_execution(db, document, task, background_tasks=background_tasks)
        return True

    task.error_message = PDF_QUEUE_WAITING_MESSAGE
    task.celery_task_id = None
    db.add(task)
    db.commit()
    db.refresh(task)
    return dispatch_next_pdf_task(db, background_tasks=background_tasks) == task.id


def dispatch_next_pdf_task(db: Session, *, background_tasks=None) -> int | None:
    with _pdf_dispatch_lock():
        if _has_active_pdf_task(db):
            return None

        task = _next_pending_pdf_task(db)
        if task is None:
            return None

        document = db.get(KnowledgeDocument, task.document_id)
        if document is None or not _is_pdf_document(document):
            return None

        _start_task_execution(db, document, task, background_tasks=background_tasks)
        return task.id


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
    elif (
        result.state == "PENDING"
        and task.status == DocumentStatus.PENDING
        and _is_pdf_document(task.document)
    ):
        updated_at = _normalize_timestamp(task.updated_at)
        now = datetime.now(UTC)
        if updated_at and now - updated_at >= timedelta(seconds=STALE_PENDING_TASK_SECONDS):
            task.celery_task_id = None
            task.error_message = PDF_QUEUE_WAITING_MESSAGE
    db.add(task)
    db.commit()
    db.refresh(task)
    if (
        task.status == DocumentStatus.PENDING
        and task.celery_task_id is None
        and _is_pdf_document(task.document)
        and task.error_message == PDF_QUEUE_WAITING_MESSAGE
    ):
        dispatch_next_pdf_task(db)
        db.refresh(task)
    return task


def run_ingest_pipeline(document_id: int, task_id: int, celery_task=None) -> None:
    db = SessionLocal()
    should_dispatch_next_pdf = False
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
        source_path = DocumentBackupService(settings).resolve_path(document.file_path)
        extracted = rag_service.extract_content(
            str(source_path),
            document.mime_type,
            document_id=document.id,
            task_id=task.id,
            resource_type=document.resource_type,
        )
        _ensure_gpu_requirement_satisfied(extracted)
        task, document = _ensure_not_cancelled(db, task_id, document_id)
        _update_status(
            db,
            task,
            document,
            progress=25,
            status=DocumentStatus.PROCESSING,
            error=_build_extraction_message(extracted),
            celery_task=celery_task,
        )
        chunks = rag_service.prepare_document_chunks(
            document,
            extracted.text,
            assets=extracted.assets,
            parsed_pdf=extracted.parsed_pdf,
            parser_backend=extracted.parser_backend,
            parser_provenance=extracted.parser_provenance,
            source_format=extracted.source_format,
        )
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
            error=_build_completion_message(document, chunks, extracted.parsed_pdf),
            celery_task=celery_task,
        )
        should_dispatch_next_pdf = _is_pdf_document(document)
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
                should_dispatch_next_pdf = _is_pdf_document(document)
    except RETRIABLE_INGEST_EXCEPTIONS as exc:
        if "db" in locals():
            task, document = _reload_task_document(db, task_id, document_id)
            if task and document:
                rag_service.clear_document_artifacts(document.id)
                _update_status(
                    db,
                    task,
                    document,
                    progress=min(task.progress or 0, 99),
                    status=DocumentStatus.PROCESSING if celery_task is not None else DocumentStatus.FAILED,
                    error=f"解析器暂时不可用，准备重试：{exc}",
                    celery_task=celery_task,
                )
        raise
    except (MineruGpuPreflightError, MineruGpuRuntimeError, GPUProofFailedError) as exc:
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
                    error=_build_gpu_failure_message(exc),
                    celery_task=celery_task,
                )
                should_dispatch_next_pdf = _is_pdf_document(document)
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
                    should_dispatch_next_pdf = _is_pdf_document(document)
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
                should_dispatch_next_pdf = _is_pdf_document(document)
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
                should_dispatch_next_pdf = _is_pdf_document(document)
    finally:
        if should_dispatch_next_pdf:
            dispatch_next_pdf_task(db)
        db.close()


@celery_app.task(
    bind=True,
    soft_time_limit=settings.ingest_soft_time_limit_seconds,
    time_limit=settings.ingest_hard_time_limit_seconds,
)
def ingest_document_task(self, document_id: int, task_id: int) -> None:
    try:
        run_ingest_pipeline(document_id, task_id, celery_task=self)
    except RETRIABLE_INGEST_EXCEPTIONS as exc:
        if self.request.retries < 1:
            raise self.retry(exc=exc, countdown=5)
        raise


def enqueue_ingest_task(document_id: int, task_id: int) -> str | None:
    try:
        result = ingest_document_task.delay(document_id, task_id)
    except Exception:
        return None
    return result.id


def sync_task_state(task: ImportTask, db: Session) -> ImportTask:
    return _sync_task_from_result(db, task)
