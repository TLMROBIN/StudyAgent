from pathlib import Path
from types import SimpleNamespace

import pytest
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


def test_bulk_update_documents_updates_multiple_rows_and_preserves_unspecified_fields(monkeypatch):
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
            chapter="第二章 电场",
            created_by=teacher.id,
        )
        second = KnowledgeDocument(
            subject="物理",
            filename="set-b.txt",
            file_path="/tmp/set-b.txt",
            mime_type="text/plain",
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


def test_update_document_preserves_parser_provenance_after_sync(tmp_path, monkeypatch):
    session_local = build_session()
    session = session_local()
    try:
        real_rag_service = build_real_rag_service(tmp_path)
        monkeypatch.setattr(knowledge_router, "rag_service", real_rag_service)

        teacher = create_teacher(session)
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.pdf",
            file_path=str(tmp_path / "questions.pdf"),
            mime_type="application/pdf",
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
                    "source_format": "pdf",
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
        assert row.metadata_json.get("source_format") == "pdf"
        assert row.metadata_json.get("source_locator") == "page:1/question:1"
        assert row.metadata_json.get("image_expectation") == "required"
        assert row.metadata_json.get("image_binding_status") == "missing_required"
        assert row.metadata_json.get("quality_flags") == ["missing_required_image"]
        assert row.metadata_json.get("question_uid") == f"{document.id}:page:1/question:1"
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
