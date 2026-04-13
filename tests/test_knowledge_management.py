import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings
from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.knowledge import DocumentStatus, ImportTask, KnowledgeChunk, KnowledgeDocument
from backend.models.user import User, UserRole
from backend.routers import knowledge as knowledge_router
from backend.services.embed_service import EmbedService
from backend.services.rag_service import RagService
from backend.services.vector_store_service import VectorStoreService
from backend.tasks.ingest import LEGACY_PDF_QUEUE_WAITING_MESSAGE, PDF_QUEUE_WAITING_MESSAGE


def build_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_local = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_local


def build_request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def build_real_rag_service(tmp_path: Path) -> RagService:
    settings = Settings(
        CHROMADB_MODE="persistent",
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        CHROMADB_COLLECTION_PREFIX="studyagent-knowledge-test",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        UPLOAD_PATH=str(tmp_path / "uploads"),
        EMBEDDING_MODEL_NAME="BAAI/bge-m3",
        EMBEDDING_BACKEND="hash",
        EMBEDDING_DEVICE="cpu",
        EMBEDDING_FALLBACK_TO_HASH=True,
    )
    embedder = EmbedService(settings)
    vector_store = VectorStoreService(settings, embedder)
    return RagService(settings=settings, embedder=embedder, vector_store=vector_store)


class FakeRagService:
    def __init__(self) -> None:
        self.deleted_documents: list[int] = []
        self.synced_documents: list[int] = []
        self.cleared_artifacts: list[int] = []
        self.upserted_chunks: list[tuple[str, list[int]]] = []
        self.vector_store = SimpleNamespace(
            upsert_chunks=lambda subject, rows: self.upserted_chunks.append(
                (subject, [row.id for row in rows])
            )
        )

    def purge_document_index(self, db: Session, document: KnowledgeDocument) -> None:
        self.deleted_documents.append(document.id)
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        db.commit()

    def sync_document_metadata(self, db: Session, document: KnowledgeDocument) -> None:
        self.synced_documents.append(document.id)

    def clear_document_artifacts(self, document_id: int) -> None:
        self.cleared_artifacts.append(document_id)

    def document_asset_dir(self, document_id: int) -> Path:
        return Path("/tmp") / "studyagent-test-assets" / str(document_id)

    def _apply_metadata_layers(self, metadata: dict) -> dict:
        return metadata


class FakeUploadFile:
    def __init__(
        self, filename: str, payload: bytes, content_type: str | None = None
    ) -> None:
        self.filename = filename
        self._payload = payload
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._payload


def create_teacher(session: Session) -> User:
    teacher = User(
        username="teacher1",
        full_name="教师",
        role=UserRole.TEACHER,
        password_hash="hash",
    )
    session.add(teacher)
    session.commit()
    session.refresh(teacher)
    return teacher


def configure_upload_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list]:
    settings = Settings(
        ALLOWED_UPLOAD_EXTENSIONS=".pdf,.docx,.txt,.md,.tex",
        ALLOWED_UPLOAD_MIME_TYPES=(
            "application/pdf,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
            "text/plain,"
            "text/markdown,"
            "text/x-markdown,"
            "text/x-tex,"
            "application/x-tex"
        ),
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        UPLOAD_PATH=str(tmp_path / "uploads"),
    )
    settings.ensure_storage()
    monkeypatch.setattr(knowledge_router, "settings", settings)
    monkeypatch.setattr(knowledge_router.auto_tag_service, "auto_tag", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        knowledge_router.auto_tag_service,
        "match_textbook_structure",
        lambda *args, **kwargs: {"chapter": None, "section": None},
    )
    dispatched: list[tuple[str, int]] = []
    audit_details: list[dict] = []
    monkeypatch.setattr(
        knowledge_router.audit_service,
        "log",
        lambda *args, **kwargs: audit_details.append(kwargs["detail"]),
    )
    monkeypatch.setattr(
        knowledge_router,
        "dispatch_import_task",
        lambda db, document, task, background_tasks=None: dispatched.append((document.filename, task.id)),
    )
    return {"dispatched": dispatched, "audit_details": audit_details}


def run_upload(session: Session, teacher: User, upload: FakeUploadFile, subject: str):
    return asyncio.run(
        knowledge_router.upload_document(
            background_tasks=BackgroundTasks(),
            subject=subject,
            resource_type="knowledge_note",
            grade=None,
            chapter=None,
            section=None,
            difficulty=None,
            tags=None,
            file=upload,
            db=session,
            current_user=teacher,
            request=build_request(),
        )
    )


def run_upload_with_metadata(
    session: Session,
    teacher: User,
    upload: FakeUploadFile,
    *,
    subject: str,
    resource_type: str,
    chapter: str | None = None,
    section: str | None = None,
):
    return asyncio.run(
        knowledge_router.upload_document(
            background_tasks=BackgroundTasks(),
            subject=subject,
            resource_type=resource_type,
            grade=None,
            chapter=chapter,
            section=section,
            difficulty=None,
            tags=None,
            file=upload,
            db=session,
            current_user=teacher,
            request=build_request(),
        )
    )


@pytest.mark.parametrize(
    ("filename", "content_type", "expected_mime_type"),
    [
        ("notes.md", None, "text/markdown"),
        ("notes.md", "application/octet-stream", "text/markdown"),
        ("notes.txt", "application/octet-stream", "text/plain"),
        (
            "notes.docx",
            None,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "notes.docx",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ],
)
def test_upload_document_accepts_supported_files_with_generic_or_missing_mime(
    tmp_path, monkeypatch, filename, content_type, expected_mime_type
):
    session_local = build_session()
    session = session_local()
    try:
        upload_context = configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(filename=filename, payload=b"demo content", content_type=content_type)

        result = run_upload(session, teacher, upload, subject="数学")

        document = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == filename)
        )
        assert document is not None
        assert document.mime_type == expected_mime_type
        task = session.scalar(select(ImportTask).where(ImportTask.document_id == document.id))
        assert task is not None
        assert upload_context["dispatched"] == [(filename, task.id)]
        assert upload_context["audit_details"][-1]["raw_mime"] == (content_type or "")
        assert upload_context["audit_details"][-1]["effective_mime"] == expected_mime_type
        assert result.document_id == document.id
    finally:
        session.close()


def test_upload_document_preserves_explicit_allowlisted_mime(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        upload_context = configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="notes.md",
            payload=b"# markdown",
            content_type="text/plain",
        )

        run_upload(session, teacher, upload, subject="语文")

        document = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == "notes.md")
        )
        assert document is not None
        assert document.mime_type == "text/plain"
        assert upload_context["audit_details"][-1]["raw_mime"] == "text/plain"
        assert upload_context["audit_details"][-1]["effective_mime"] == "text/plain"
    finally:
        session.close()


@pytest.mark.parametrize("content_type", ["image/png", "application/pdf"])
def test_upload_document_rejects_conflicting_mime_for_supported_extension(
    tmp_path, monkeypatch, content_type
):
    session_local = build_session()
    session = session_local()
    try:
        configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="notes.md",
            payload=b"# markdown",
            content_type=content_type,
        )

        with pytest.raises(HTTPException) as exc_info:
            run_upload(session, teacher, upload, subject="英语")

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Unsupported MIME type"
        assert session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.filename == "notes.md")) is None
    finally:
        session.close()


def test_upload_document_rejects_unsupported_extension(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="notes.exe",
            payload=b"MZ",
            content_type="application/octet-stream",
        )

        with pytest.raises(HTTPException) as exc_info:
            run_upload(session, teacher, upload, subject="物理")

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Unsupported file type"
        assert session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.filename == "notes.exe")) is None
    finally:
        session.close()


def test_upload_document_rejects_non_docx_question_resource(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="questions.pdf",
            payload=b"%PDF-1.4",
            content_type="application/pdf",
        )

        with pytest.raises(HTTPException) as exc_info:
            run_upload_with_metadata(
                session,
                teacher,
                upload,
                subject="物理",
                resource_type="question_set",
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Question resources require DOCX files"
        assert session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == "questions.pdf")
        ) is None
    finally:
        session.close()


def test_upload_document_rejects_legacy_mathtype_question_docx_before_task_creation(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        configure_upload_router(tmp_path, monkeypatch)
        monkeypatch.setattr(
            knowledge_router.rag_service,
            "ensure_question_resource_docx_supported",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                knowledge_router.UnsupportedQuestionDocxError(
                    "检测到 MathType 类 legacy 公式，当前不支持；请改用微软公式（OMML）后重新导入"
                )
            ),
        )
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="questions.docx",
            payload=b"docx payload",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        with pytest.raises(HTTPException) as exc_info:
            run_upload_with_metadata(
                session,
                teacher,
                upload,
                subject="物理",
                resource_type="question_set",
            )

        assert exc_info.value.status_code == 400
        assert "MathType 类 legacy 公式" in exc_info.value.detail
        assert session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == "questions.docx")
        ) is None
        assert session.scalar(select(ImportTask)) is None
    finally:
        session.close()


def test_upload_document_rejects_exercise_docx_without_resolved_chapter(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="exercise.docx",
            payload=b"docx payload",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        with pytest.raises(HTTPException) as exc_info:
            run_upload_with_metadata(
                session,
                teacher,
                upload,
                subject="物理",
                resource_type="exercise",
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Chapter is required for exercise DOCX uploads"
        assert session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == "exercise.docx")
        ) is None
        assert session.scalar(select(ImportTask)) is None
    finally:
        session.close()


def test_upload_document_accepts_exercise_docx_with_manual_chapter(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        upload_context = configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="exercise.docx",
            payload=b"docx payload",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        result = run_upload_with_metadata(
            session,
            teacher,
            upload,
            subject="物理",
            resource_type="exercise",
            chapter="第二章 机械运动",
        )

        document = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == "exercise.docx")
        )
        assert document is not None
        assert document.resource_type == "exercise"
        assert document.chapter == "第二章 机械运动"
        task = session.scalar(select(ImportTask).where(ImportTask.document_id == document.id))
        assert task is not None
        assert upload_context["dispatched"] == [("exercise.docx", task.id)]
        assert result.document_id == document.id
    finally:
        session.close()


def test_upload_document_accepts_question_set_docx_without_resolved_chapter(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        upload_context = configure_upload_router(tmp_path, monkeypatch)
        teacher = create_teacher(session)
        upload = FakeUploadFile(
            filename="question-set.docx",
            payload=b"docx payload",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        result = run_upload_with_metadata(
            session,
            teacher,
            upload,
            subject="物理",
            resource_type="question_set",
        )

        document = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.filename == "question-set.docx")
        )
        assert document is not None
        assert document.resource_type == "question_set"
        assert document.chapter is None
        task = session.scalar(select(ImportTask).where(ImportTask.document_id == document.id))
        assert task is not None
        assert upload_context["dispatched"] == [("question-set.docx", task.id)]
        assert result.document_id == document.id
    finally:
        session.close()


def test_delete_task_removes_failed_record_only():
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="failed.txt",
            file_path="/tmp/failed.txt",
            mime_type="text/plain",
            size_bytes=128,
            status=DocumentStatus.FAILED,
            error_message="导入失败",
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        task = ImportTask(
            document_id=document.id,
            status=DocumentStatus.FAILED,
            progress=100,
            error_message="任务失败",
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        knowledge_router.delete_task(task.id, db=session, current_user=teacher, request=build_request())

        assert session.get(ImportTask, task.id) is None
        assert session.get(KnowledgeDocument, document.id) is not None
    finally:
        session.close()


def test_delete_task_rejects_active_task():
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="数学",
            filename="processing.txt",
            file_path="/tmp/processing.txt",
            mime_type="text/plain",
            size_bytes=64,
            status=DocumentStatus.PROCESSING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        task = ImportTask(
            document_id=document.id,
            status=DocumentStatus.PROCESSING,
            progress=35,
            error_message="处理中",
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.delete_task(task.id, db=session, current_user=teacher, request=build_request())

        assert exc_info.value.status_code == 409
        assert session.get(ImportTask, task.id) is not None
    finally:
        session.close()


def test_cancel_waiting_pdf_task_promotes_next_pending_pdf(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="queued.pdf",
            file_path="/tmp/queued.pdf",
            mime_type="application/pdf",
            size_bytes=64,
            status=DocumentStatus.PENDING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        task = ImportTask(
            document_id=document.id,
            status=DocumentStatus.PENDING,
            progress=0,
            error_message=PDF_QUEUE_WAITING_MESSAGE,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        promoted: list[bool] = []
        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: promoted.append(True) or None)

        result = knowledge_router.cancel_task(task.id, db=session, current_user=teacher)

        assert result.status == DocumentStatus.CANCELLED
        assert promoted == [True]
    finally:
        session.close()


def test_list_tasks_promotes_waiting_pdf_queue(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="queued.pdf",
            file_path="/tmp/queued.pdf",
            mime_type="application/pdf",
            size_bytes=64,
            status=DocumentStatus.PENDING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        task = ImportTask(
            document_id=document.id,
            status=DocumentStatus.PENDING,
            progress=0,
            error_message=PDF_QUEUE_WAITING_MESSAGE,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        promoted: list[bool] = []
        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: promoted.append(True) or None)

        result = knowledge_router.list_tasks(db=session, current_user=teacher)

        assert result
        assert promoted == [True]
    finally:
        session.close()


def test_list_tasks_without_limit_returns_more_than_100_rows(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="数学",
            filename="bulk.txt",
            file_path="/tmp/bulk.txt",
            mime_type="text/plain",
            size_bytes=64,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add_all(
            [
                ImportTask(
                    document_id=document.id,
                    status=DocumentStatus.COMPLETED,
                    progress=100,
                    error_message=f"导入完成-{index}",
                )
                for index in range(101)
            ]
        )
        session.commit()

        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: None)

        result = knowledge_router.list_tasks(db=session, current_user=teacher)

        assert len(result) == 101
    finally:
        session.close()


def test_list_tasks_paginated_returns_total_and_summary(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="数学",
            filename="paged.txt",
            file_path="/tmp/paged.txt",
            mime_type="text/plain",
            size_bytes=64,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add_all(
            [
                ImportTask(document_id=document.id, status=DocumentStatus.COMPLETED, progress=100),
                ImportTask(document_id=document.id, status=DocumentStatus.FAILED, progress=100),
                ImportTask(document_id=document.id, status=DocumentStatus.PENDING, progress=0),
            ]
        )
        session.commit()

        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: None)

        result = knowledge_router.list_tasks(
            db=session,
            current_user=teacher,
            page=1,
            page_size=2,
        )

        assert result.total == 3
        assert result.page == 1
        assert result.page_size == 2
        assert len(result.items) == 2
        assert result.summary.total == 3
        assert result.summary.active == 1
        assert result.summary.completed == 1
        assert result.summary.failed == 1
    finally:
        session.close()


def test_cancel_waiting_pdf_task_accepts_legacy_queue_message(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="queued.pdf",
            file_path="/tmp/queued.pdf",
            mime_type="application/pdf",
            size_bytes=64,
            status=DocumentStatus.PENDING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        task = ImportTask(
            document_id=document.id,
            status=DocumentStatus.PENDING,
            progress=0,
            error_message=LEGACY_PDF_QUEUE_WAITING_MESSAGE,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        promoted: list[bool] = []
        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: promoted.append(True) or None)

        result = knowledge_router.cancel_task(task.id, db=session, current_user=teacher)

        assert result.status == DocumentStatus.CANCELLED
        assert promoted == [True]
    finally:
        session.close()


def test_list_documents_marks_orphan_pending_document_as_failed():
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="英语",
            filename="orphan.txt",
            file_path="/tmp/orphan.txt",
            mime_type="text/plain",
            size_bytes=32,
            status=DocumentStatus.PENDING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        rows = knowledge_router.list_documents(db=session, current_user=teacher)

        assert len(rows) == 1
        assert rows[0].status == DocumentStatus.FAILED
        assert rows[0].error_message == "未找到关联导入任务，可删除后重新上传"
        refreshed = session.get(KnowledgeDocument, document.id)
        assert refreshed is not None
        assert refreshed.status == DocumentStatus.FAILED
    finally:
        session.close()


def test_list_documents_paginated_returns_total_and_active_flag(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        active_document = KnowledgeDocument(
            subject="英语",
            filename="active.txt",
            file_path="/tmp/active.txt",
            mime_type="text/plain",
            size_bytes=32,
            status=DocumentStatus.PENDING,
            created_by=teacher.id,
        )
        completed_document = KnowledgeDocument(
            subject="英语",
            filename="done.txt",
            file_path="/tmp/done.txt",
            mime_type="text/plain",
            size_bytes=32,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add_all([active_document, completed_document])
        session.commit()
        session.refresh(active_document)
        session.refresh(completed_document)

        session.add(
            ImportTask(
                document_id=active_document.id,
                status=DocumentStatus.PENDING,
                progress=0,
                error_message="处理中",
            )
        )
        session.commit()

        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: None)

        result = knowledge_router.list_documents(
            db=session,
            current_user=teacher,
            page=1,
            page_size=10,
            subject="英语",
        )

        assert result.total == 2
        assert result.summary.total == 2
        assert result.summary.active == 1
        assert len(result.items) == 2
        active_row = next(item for item in result.items if item.id == active_document.id)
        done_row = next(item for item in result.items if item.id == completed_document.id)
        assert active_row.has_active_task is True
        assert done_row.has_active_task is False
    finally:
        session.close()


def test_list_documents_paginated_supports_chapter_section_and_tag_filters(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        session.add_all(
            [
                KnowledgeDocument(
                    subject="物理",
                    filename="motion.txt",
                    file_path="/tmp/motion.txt",
                    mime_type="text/plain",
                    size_bytes=32,
                    status=DocumentStatus.COMPLETED,
                    chapter="第二章 机械运动",
                    section="2.1 匀变速直线运动",
                    tags_json=["运动", "速度"],
                    created_by=teacher.id,
                ),
                KnowledgeDocument(
                    subject="物理",
                    filename="force.txt",
                    file_path="/tmp/force.txt",
                    mime_type="text/plain",
                    size_bytes=32,
                    status=DocumentStatus.COMPLETED,
                    chapter="第三章 牛顿运动定律",
                    section="3.1 牛顿第一定律",
                    tags_json=["受力", "惯性"],
                    created_by=teacher.id,
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: None)

        result = knowledge_router.list_documents(
            db=session,
            current_user=teacher,
            page=1,
            page_size=10,
            chapter="机械运动",
            section="匀变速",
            tag="速度",
        )

        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].filename == "motion.txt"
    finally:
        session.close()


def test_metadata_suggestions_returns_ranked_existing_values(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        session.add_all(
            [
                KnowledgeDocument(
                    subject="数学",
                    filename="a.txt",
                    file_path="/tmp/a.txt",
                    mime_type="text/plain",
                    size_bytes=12,
                    status=DocumentStatus.COMPLETED,
                    chapter="第二章 函数",
                    section="2.1 函数与映射",
                    tags_json=["函数", "映射"],
                    created_by=teacher.id,
                ),
                KnowledgeDocument(
                    subject="数学",
                    filename="b.txt",
                    file_path="/tmp/b.txt",
                    mime_type="text/plain",
                    size_bytes=12,
                    status=DocumentStatus.COMPLETED,
                    chapter="第二章 函数综合",
                    section="2.2 函数单调性",
                    tags_json=["函数", "单调性"],
                    created_by=teacher.id,
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr(knowledge_router, "dispatch_next_pdf_task", lambda db: None)

        chapter_values = knowledge_router.list_metadata_suggestions(
            field="chapter",
            query="函数",
            subject="数学",
            db=session,
            current_user=teacher,
            limit=10,
        )
        tag_values = knowledge_router.list_metadata_suggestions(
            field="tag",
            query="函",
            subject="数学",
            db=session,
            current_user=teacher,
            limit=10,
        )

        assert chapter_values == ["第二章 函数", "第二章 函数综合"]
        assert tag_values == ["函数"]
    finally:
        session.close()


def test_delete_document_removes_file_chunks_and_related_tasks(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        source_file = tmp_path / "knowledge.txt"
        source_file.write_text("牛顿第二定律。", encoding="utf-8")

        document = KnowledgeDocument(
            subject="物理",
            filename="knowledge.txt",
            file_path=str(source_file),
            mime_type="text/plain",
            size_bytes=source_file.stat().st_size,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add(
            KnowledgeChunk(
                document_id=document.id,
                subject=document.subject,
                chunk_index=0,
                content="牛顿第二定律内容",
                metadata_json={"document_id": document.id},
            )
        )
        session.add(
            ImportTask(
                document_id=document.id,
                status=DocumentStatus.COMPLETED,
                progress=100,
                error_message="导入完成",
            )
        )
        session.commit()

        task = session.scalar(select(ImportTask).where(ImportTask.document_id == document.id))
        assert task is not None

        knowledge_router.delete_document(document.id, db=session, current_user=teacher, request=build_request())

        assert fake_rag_service.cleared_artifacts == [document.id]
        assert fake_rag_service.deleted_documents == [document.id]
        assert not source_file.exists()
        assert session.get(KnowledgeDocument, document.id) is None
        assert session.get(ImportTask, task.id) is None
        assert session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)) is None
    finally:
        session.close()


def test_list_document_chunks_returns_question_metadata():
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add(
            KnowledgeChunk(
                document_id=document.id,
                subject=document.subject,
                chunk_index=0,
                content="第1题\n\n题目：如图所示...",
                metadata_json={
                    "document_id": document.id,
                    "chunk_kind": "question_item",
                    "question_number": "1",
                    "question_text": "如图所示...",
                    "answer_text": "A",
                    "explanation_text": "由受力分析可得",
                    "contains_images": True,
                    "image_count": 1,
                    "asset_refs": [
                        {
                            "asset_id": "image-001",
                            "filename": "image-001.png",
                            "content_type": "image/png",
                            "url": "/api/knowledge/documents/1/assets/image-001.png",
                            "title": "受力图",
                            "description": None,
                        }
                    ],
                },
            )
        )
        session.commit()

        result = knowledge_router.list_document_chunks(document.id, db=session, current_user=teacher)

        assert len(result) == 1
        assert result[0].question_number == "1"
        assert result[0].answer_text == "A"
        assert result[0].explanation_text == "由受力分析可得"
        assert result[0].contains_images is True
        assert result[0].image_count == 1
        assert result[0].assets[0].filename == "image-001.png"
        assert result[0].assets[0].url == "/api/knowledge/documents/1/assets/image-001.png"
        assert result[0].assets[0].title == "受力图"
    finally:
        session.close()


def test_list_questions_returns_question_rows_with_filters():
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        question_document = KnowledgeDocument(
            subject="物理",
            filename="question-set.docx",
            file_path="/tmp/question-set.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            tags_json=["文档标签"],
            created_by=teacher.id,
        )
        note_document = KnowledgeDocument(
            subject="物理",
            filename="notes.txt",
            file_path="/tmp/notes.txt",
            mime_type="text/plain",
            size_bytes=64,
            status=DocumentStatus.COMPLETED,
            resource_type="knowledge_note",
            created_by=teacher.id,
        )
        session.add_all([question_document, note_document])
        session.commit()
        session.refresh(question_document)
        session.refresh(note_document)

        session.add_all(
            [
                KnowledgeChunk(
                    document_id=question_document.id,
                    subject="物理",
                    chunk_index=0,
                    content="第1题\n\n题目：匀速直线运动求位移。",
                    metadata_json={
                        "document_id": question_document.id,
                        "resource_type": "question_set",
                        "chunk_kind": "question_item",
                        "question_number": "1",
                        "question_text": "匀速直线运动求位移，位移公式为 $s=vt$，如图[[asset:image-001]]。",
                        "difficulty": "advanced",
                        "chapter": "第二章 机械运动",
                        "tags": ["速度", "位移"],
                        "contains_images": True,
                        "image_count": 1,
                        "asset_refs": [
                            {
                                "asset_id": "image-001",
                                "filename": "image-001.png",
                                "content_type": "image/png",
                                "url": "/api/knowledge/documents/1/assets/image-001.png",
                            }
                        ],
                    },
                ),
                KnowledgeChunk(
                    document_id=question_document.id,
                    subject="物理",
                    chunk_index=1,
                    content="第2题\n\n题目：牛顿第二定律。",
                    is_disabled=True,
                    metadata_json={
                        "document_id": question_document.id,
                        "resource_type": "question_set",
                        "chunk_kind": "question_item",
                        "question_number": "2",
                        "question_text": "牛顿第二定律。",
                        "difficulty": "basic",
                        "chapter": "第三章 牛顿运动定律",
                        "tags": ["受力分析"],
                    },
                ),
                KnowledgeChunk(
                    document_id=note_document.id,
                    subject="物理",
                    chunk_index=0,
                    content="知识点讲义",
                    metadata_json={
                        "document_id": note_document.id,
                        "resource_type": "knowledge_note",
                    },
                ),
            ]
        )
        session.commit()

        result = knowledge_router.list_questions(
            db=session,
            current_user=teacher,
            page=1,
            page_size=10,
            subject="物理",
            difficulty="advanced",
            chapter="机械运动",
            tag="速度",
            disabled=False,
            keyword="位移",
        )

        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].document_filename == "question-set.docx"
        assert result.items[0].question_number == "1"
        assert "$s=vt$" in result.items[0].question_text
        assert result.items[0].is_disabled is False
        assert result.items[0].contains_images is True
        assert result.items[0].image_count == 1
        assert result.items[0].assets[0].filename == "image-001.png"
    finally:
        session.close()


def test_list_question_metadata_suggestions_uses_question_scope():
    session_local = build_session()
    session = session_local()
    try:
        teacher = create_teacher(session)
        question_document = KnowledgeDocument(
            subject="数学",
            filename="question-set.docx",
            file_path="/tmp/question-set.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            chapter="文档章节",
            tags_json=["文档标签"],
            created_by=teacher.id,
        )
        session.add(question_document)
        session.commit()
        session.refresh(question_document)

        session.add_all(
            [
                KnowledgeChunk(
                    document_id=question_document.id,
                    subject="数学",
                    chunk_index=0,
                    content="第1题",
                    metadata_json={
                        "document_id": question_document.id,
                        "resource_type": "question_set",
                        "chunk_kind": "question_item",
                        "question_number": "1",
                        "question_text": "函数单调性",
                        "chapter": "第二章 函数",
                        "tags": ["单调性", "函数"],
                    },
                ),
                KnowledgeChunk(
                    document_id=question_document.id,
                    subject="数学",
                    chunk_index=1,
                    content="第2题",
                    metadata_json={
                        "document_id": question_document.id,
                        "resource_type": "question_set",
                        "chunk_kind": "question_item",
                        "question_number": "2",
                        "question_text": "函数奇偶性",
                        "chapter": "第二章 函数综合",
                        "tags": ["奇偶性"],
                    },
                ),
            ]
        )
        session.commit()

        chapter_values = knowledge_router.list_question_metadata_suggestions(
            field="chapter",
            query="函数",
            subject="数学",
            resource_type="question_set",
            db=session,
            current_user=teacher,
            limit=10,
        )
        tag_values = knowledge_router.list_question_metadata_suggestions(
            field="tag",
            query="函",
            subject="数学",
            resource_type="question_set",
            db=session,
            current_user=teacher,
            limit=10,
        )

        assert chapter_values == ["第二章 函数", "第二章 函数综合"]
        assert tag_values == ["函数"]
    finally:
        session.close()


def test_update_question_metadata_updates_chunk_only_and_reindexes(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            chapter="第二章 机械运动",
            tags_json=["文档标签"],
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        row = KnowledgeChunk(
            document_id=document.id,
            subject=document.subject,
            chunk_index=0,
            content="第1题\n\n题目：匀速直线运动求位移。",
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "chunk_kind": "question_item",
                "question_number": "1",
                "question_text": "匀速直线运动求位移。",
                "answer_text": "B",
                "explanation_text": "依据位移公式。",
                "chapter": "第二章 机械运动",
                "section": "2.1 匀速直线运动",
                "difficulty": "basic",
                "tags": ["速度"],
            },
        )
        session.add(row)
        session.commit()
        session.refresh(row)

        result = knowledge_router.update_question(
            row.id,
            payload=knowledge_router.KnowledgeQuestionUpdate(
                chapter="第二章 机械运动综合",
                section="2.2 综合应用",
                difficulty="advanced",
                tags=["速度", "位移"],
            ),
            db=session,
            current_user=teacher,
            request=build_request(),
        )

        refreshed = session.get(KnowledgeChunk, row.id)
        assert refreshed is not None
        assert result.chapter == "第二章 机械运动综合"
        assert result.section == "2.2 综合应用"
        assert result.difficulty == "advanced"
        assert result.tags == ["速度", "位移"]
        assert refreshed.metadata_json["question_text"] == "匀速直线运动求位移。"
        assert refreshed.metadata_json["answer_text"] == "B"
        assert refreshed.metadata_json["explanation_text"] == "依据位移公式。"
        assert document.chapter == "第二章 机械运动"
        assert fake_rag_service.upserted_chunks == [("物理", [row.id])]
    finally:
        session.close()


def test_disable_and_restore_question_keeps_row_visible_to_staff(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        monkeypatch.setattr(knowledge_router, "rag_service", FakeRagService())
        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="化学",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        row = KnowledgeChunk(
            document_id=document.id,
            subject=document.subject,
            chunk_index=0,
            content="第1题\n\n题目：化学平衡。",
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "chunk_kind": "question_item",
                "question_number": "1",
                "question_text": "化学平衡。",
            },
        )
        session.add(row)
        session.commit()
        session.refresh(row)

        disabled = knowledge_router.disable_question(
            row.id,
            db=session,
            current_user=teacher,
            request=build_request(),
        )
        disabled_rows = knowledge_router.list_questions(
            db=session,
            current_user=teacher,
            page=1,
            page_size=10,
            disabled=True,
        )
        restored = knowledge_router.restore_question(
            row.id,
            db=session,
            current_user=teacher,
            request=build_request(),
        )

        assert disabled.is_disabled is True
        assert disabled_rows.total == 1
        assert disabled_rows.items[0].id == row.id
        assert restored.is_disabled is False
        assert session.get(KnowledgeChunk, row.id).is_disabled is False
    finally:
        session.close()


def test_get_document_asset_returns_saved_file(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="数学",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=512,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        asset_dir = fake_rag_service.document_asset_dir(document.id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        asset_path = asset_dir / "image-001.png"
        asset_path.write_bytes(b"png")

        response = knowledge_router.get_document_asset(
            document.id,
            "image-001.png",
            db=session,
            current_user=teacher,
        )

        assert response.path == asset_path
        assert response.media_type == "image/png"
    finally:
        session.close()


def test_update_document_metadata_persists_fields_and_triggers_sync(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="meta.docx",
            file_path="/tmp/meta.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=12,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        payload = knowledge_router.KnowledgeDocumentUpdate(
            resource_type="question_set",
            grade=2,
            chapter="第二章 机械运动",
            section="2.1 匀变速直线运动",
            difficulty="advanced",
            tags=["运动", "速度"],
        )
        result = knowledge_router.update_document(
            document.id,
            payload=payload,
            db=session,
            current_user=teacher,
            request=build_request(),
        )

        assert result.resource_type == "question_set"
        assert result.grade == 2
        assert result.chapter == "第二章 机械运动"
        assert result.difficulty == "advanced"
        assert result.tags == ["运动", "速度"]
        assert fake_rag_service.synced_documents == [document.id]
    finally:
        session.close()


def test_update_document_rejects_non_docx_resource_type_conversion(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="meta.txt",
            file_path="/tmp/meta.txt",
            mime_type="text/plain",
            size_bytes=12,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.update_document(
                document.id,
                payload=knowledge_router.KnowledgeDocumentUpdate(
                    resource_type="question_set"
                ),
                db=session,
                current_user=teacher,
                request=build_request(),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Question resources require DOCX documents"
        assert fake_rag_service.synced_documents == []
    finally:
        session.close()


def test_bulk_update_documents_updates_multiple_rows_and_preserves_unspecified_fields(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        first = KnowledgeDocument(
            subject="物理",
            filename="set-a.docx",
            file_path="/tmp/set-a.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=12,
            status=DocumentStatus.COMPLETED,
            chapter="第二章 电场",
            created_by=teacher.id,
        )
        second = KnowledgeDocument(
            subject="物理",
            filename="set-b.docx",
            file_path="/tmp/set-b.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=18,
            status=DocumentStatus.COMPLETED,
            chapter="第三章 磁场",
            created_by=teacher.id,
        )
        session.add_all([first, second])
        session.commit()
        session.refresh(first)
        session.refresh(second)

        payload = knowledge_router.KnowledgeDocumentBulkUpdate(
            document_ids=[first.id, second.id],
            resource_type="question_set",
            grade=3,
            difficulty="challenge",
            tags=["电学", "综合"],
        )
        result = knowledge_router.bulk_update_documents(
            payload=payload,
            db=session,
            current_user=teacher,
            request=build_request(),
        )

        assert [row.id for row in result] == [first.id, second.id]
        assert all(row.resource_type == "question_set" for row in result)
        assert all(row.grade == 3 for row in result)
        assert all(row.difficulty == "challenge" for row in result)
        assert all(row.tags == ["电学", "综合"] for row in result)
        assert result[0].chapter == "第二章 电场"
        assert result[1].chapter == "第三章 磁场"
        assert fake_rag_service.synced_documents == [first.id, second.id]
    finally:
        session.close()


def test_bulk_update_documents_rejects_non_docx_question_resource_conversion(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        first = KnowledgeDocument(
            subject="物理",
            filename="set-a.txt",
            file_path="/tmp/set-a.txt",
            mime_type="text/plain",
            size_bytes=12,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        second = KnowledgeDocument(
            subject="物理",
            filename="set-b.docx",
            file_path="/tmp/set-b.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=18,
            status=DocumentStatus.COMPLETED,
            created_by=teacher.id,
        )
        session.add_all([first, second])
        session.commit()
        session.refresh(first)
        session.refresh(second)

        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.bulk_update_documents(
                payload=knowledge_router.KnowledgeDocumentBulkUpdate(
                    document_ids=[first.id, second.id],
                    resource_type="question_set",
                ),
                db=session,
                current_user=teacher,
                request=build_request(),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Question resources require DOCX documents"
        assert fake_rag_service.synced_documents == []
    finally:
        session.close()


def test_update_document_preserves_parser_provenance_after_sync(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        real_rag_service = build_real_rag_service(tmp_path)
        monkeypatch.setattr(knowledge_router, "rag_service", real_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.docx",
            file_path=str(tmp_path / "questions.docx"),
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add(
            KnowledgeChunk(
                document_id=document.id,
                subject=document.subject,
                chunk_index=0,
                content="第1题\n\n题目：如图所示...",
                metadata_json={
                    "document_id": document.id,
                    "resource_type": document.resource_type,
                    "chunk_kind": "question_item",
                    "question_number": "1",
                    "question_text": "如图所示...",
                    "answer_text": "A",
                    "explanation_text": "由受力分析可得",
                    "contains_images": False,
                    "image_count": 0,
                    "asset_refs": [],
                    "parser_backend": "pipeline",
                    "parser_provenance": {"runtime_artifact": "data/tasks/123/mineru-runtime.json"},
                    "source_format": "docx",
                    "source_locator": "page:1/question:1",
                    "image_expectation": "required",
                    "image_binding_status": "missing_required",
                    "quality_flags": ["missing_required_image"],
                    "question_uid": f"{document.id}:page:1/question:1",
                },
            )
        )
        session.commit()

        payload = knowledge_router.KnowledgeDocumentUpdate(
            resource_type="question_set",
            grade=2,
            chapter="第二章 机械运动",
            section="2.1 匀变速直线运动",
            difficulty="advanced",
            tags=["运动", "速度"],
        )
        knowledge_router.update_document(
            document.id,
            payload=payload,
            db=session,
            current_user=teacher,
            request=build_request(),
        )

        row = session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)).first()
        assert row is not None
        assert row.metadata_json.get("parser_backend") == "pipeline"
        assert row.metadata_json.get("parser_provenance", {}).get("runtime_artifact") == "data/tasks/123/mineru-runtime.json"
        assert row.metadata_json.get("source_format") == "docx"
        assert row.metadata_json.get("source_locator") == "page:1/question:1"
        assert row.metadata_json.get("image_expectation") == "required"
        assert row.metadata_json.get("image_binding_status") == "missing_required"
        assert row.metadata_json.get("quality_flags") == ["missing_required_image"]
        assert row.metadata_json.get("question_uid") == f"{document.id}:page:1/question:1"
    finally:
        session.close()


def test_update_document_rejects_legacy_non_docx_question_resource(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.pdf",
            file_path="/tmp/questions.pdf",
            mime_type="application/pdf",
            size_bytes=256,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.update_document(
                document.id,
                payload=knowledge_router.KnowledgeDocumentUpdate(
                    resource_type="question_set",
                    chapter="第二章 机械运动",
                ),
                db=session,
                current_user=teacher,
                request=build_request(),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Question resources require DOCX documents"
        assert fake_rag_service.synced_documents == []
    finally:
        session.close()


def test_bulk_update_documents_rejects_legacy_non_docx_question_resource(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        fake_rag_service = FakeRagService()
        monkeypatch.setattr(knowledge_router, "rag_service", fake_rag_service)

        teacher = create_teacher(session)
        legacy = KnowledgeDocument(
            subject="物理",
            filename="legacy.pdf",
            file_path="/tmp/legacy.pdf",
            mime_type="application/pdf",
            size_bytes=128,
            status=DocumentStatus.COMPLETED,
            resource_type="question_set",
            created_by=teacher.id,
        )
        session.add(legacy)
        session.commit()
        session.refresh(legacy)

        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.bulk_update_documents(
                payload=knowledge_router.KnowledgeDocumentBulkUpdate(
                    document_ids=[legacy.id],
                    resource_type="question_set",
                    tags=["综合"],
                ),
                db=session,
                current_user=teacher,
                request=build_request(),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Question resources require DOCX documents"
        assert fake_rag_service.synced_documents == []
    finally:
        session.close()


def test_bulk_update_documents_rejects_active_task(monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        monkeypatch.setattr(knowledge_router, "rag_service", FakeRagService())

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="化学",
            filename="active-bulk.txt",
            file_path="/tmp/active-bulk.txt",
            mime_type="text/plain",
            size_bytes=16,
            status=DocumentStatus.PROCESSING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add(
            ImportTask(
                document_id=document.id,
                status=DocumentStatus.PROCESSING,
                progress=20,
                error_message="处理中",
            )
        )
        session.commit()

        payload = knowledge_router.KnowledgeDocumentBulkUpdate(
            document_ids=[document.id],
            grade=2,
        )
        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.bulk_update_documents(
                payload=payload,
                db=session,
                current_user=teacher,
                request=build_request(),
            )

        assert exc_info.value.status_code == 409
        refreshed = session.get(KnowledgeDocument, document.id)
        assert refreshed is not None
        assert refreshed.grade is None
    finally:
        session.close()


def test_delete_document_rejects_when_active_task_exists(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        monkeypatch.setattr(knowledge_router, "rag_service", FakeRagService())

        teacher = create_teacher(session)
        source_file = tmp_path / "active.txt"
        source_file.write_text("进行中的文档", encoding="utf-8")

        document = KnowledgeDocument(
            subject="化学",
            filename="active.txt",
            file_path=str(source_file),
            mime_type="text/plain",
            size_bytes=source_file.stat().st_size,
            status=DocumentStatus.PROCESSING,
            created_by=teacher.id,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        session.add(
            ImportTask(
                document_id=document.id,
                status=DocumentStatus.PROCESSING,
                progress=50,
                error_message="处理中",
            )
        )
        session.commit()

        with pytest.raises(HTTPException) as exc_info:
            knowledge_router.delete_document(document.id, db=session, current_user=teacher, request=build_request())

        assert exc_info.value.status_code == 409
        assert source_file.exists()
        assert session.get(KnowledgeDocument, document.id) is not None
    finally:
        session.close()
