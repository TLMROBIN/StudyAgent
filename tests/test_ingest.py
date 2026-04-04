from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config import Settings
from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.knowledge import DocumentStatus, ImportTask, KnowledgeChunk, KnowledgeDocument
from backend.services.embed_service import EmbedService
from backend.services.rag_service import RagService
from backend.services.vector_store_service import VectorStoreService
from backend.tasks import ingest as ingest_module


def build_rag_service(tmp_path: Path) -> RagService:
    settings = Settings(
        CHROMADB_MODE="persistent",
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        CHROMADB_COLLECTION_PREFIX="studyagent-ingest-test",
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


def setup_testing_db():
    engine = create_engine("sqlite:///:memory:")
    testing_session = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return testing_session


def test_run_ingest_pipeline_marks_task_completed(tmp_path, monkeypatch):
    testing_session = setup_testing_db()
    monkeypatch.setattr(ingest_module, "SessionLocal", testing_session)
    monkeypatch.setattr(ingest_module, "rag_service", build_rag_service(tmp_path))

    source_file = tmp_path / "demo.txt"
    source_file.write_text("函数单调性需要先看定义域，然后再看自变量变化对应的函数值变化。", encoding="utf-8")

    session = testing_session()
    document = KnowledgeDocument(
        subject="数学",
        filename="demo.txt",
        file_path=str(source_file),
        mime_type="text/plain",
        size_bytes=source_file.stat().st_size,
        status=DocumentStatus.PENDING,
    )
    session.add(document)
    session.commit()
    session.refresh(document)

    task = ImportTask(document_id=document.id, status=DocumentStatus.PENDING, progress=0)
    session.add(task)
    session.commit()
    session.refresh(task)
    session.close()

    ingest_module.run_ingest_pipeline(document.id, task.id)

    verify = testing_session()
    refreshed_task = verify.get(ImportTask, task.id)
    refreshed_document = verify.get(KnowledgeDocument, document.id)
    chunk_count = len(verify.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)).all())
    assert refreshed_task is not None
    assert refreshed_document is not None
    assert refreshed_task.status == DocumentStatus.COMPLETED
    assert refreshed_task.progress == 100
    assert "导入完成" in (refreshed_task.error_message or "")
    assert refreshed_document.status == DocumentStatus.COMPLETED
    assert chunk_count >= 1
    verify.close()


def test_run_ingest_pipeline_keeps_cancelled_task_without_chunks(tmp_path, monkeypatch):
    testing_session = setup_testing_db()
    monkeypatch.setattr(ingest_module, "SessionLocal", testing_session)
    monkeypatch.setattr(ingest_module, "rag_service", build_rag_service(tmp_path))

    source_file = tmp_path / "cancelled.txt"
    source_file.write_text("化学平衡相关内容。", encoding="utf-8")

    session = testing_session()
    document = KnowledgeDocument(
        subject="化学",
        filename="cancelled.txt",
        file_path=str(source_file),
        mime_type="text/plain",
        size_bytes=source_file.stat().st_size,
        status=DocumentStatus.CANCELLED,
    )
    session.add(document)
    session.commit()
    session.refresh(document)

    task = ImportTask(
        document_id=document.id,
        status=DocumentStatus.CANCELLED,
        progress=0,
        error_message="已请求取消任务",
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    session.close()

    ingest_module.run_ingest_pipeline(document.id, task.id)

    verify = testing_session()
    refreshed_task = verify.get(ImportTask, task.id)
    refreshed_document = verify.get(KnowledgeDocument, document.id)
    chunk_count = len(verify.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)).all())
    assert refreshed_task is not None
    assert refreshed_document is not None
    assert refreshed_task.status == DocumentStatus.CANCELLED
    assert refreshed_document.status == DocumentStatus.CANCELLED
    assert chunk_count == 0
    verify.close()
