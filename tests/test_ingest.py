from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config import Settings
from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.knowledge import DocumentStatus, ImportTask, KnowledgeChunk, KnowledgeDocument
from backend.services.embed_service import EmbedService
from backend.services.mineru_service import GPUProofFailedError
from backend.services.pdf_parse_types import ExtractedAsset, PDFBlock, PDFParseResult
from backend.services.rag_service import ExtractionResult, RagService
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


def test_run_ingest_pipeline_fails_closed_when_mineru_gpu_proof_is_required_and_missing(tmp_path, monkeypatch):
    testing_session = setup_testing_db()
    monkeypatch.setattr(ingest_module, "SessionLocal", testing_session)
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "mineru"
    monkeypatch.setattr(ingest_module, "rag_service", rag_service)

    source_file = tmp_path / "scan.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    def fake_extract_content(*args, **kwargs):
        raise GPUProofFailedError("gpu proof missing")

    monkeypatch.setattr(rag_service, "extract_content", fake_extract_content)

    session = testing_session()
    document = KnowledgeDocument(
        subject="物理",
        filename="scan.pdf",
        file_path=str(source_file),
        mime_type="application/pdf",
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
    assert refreshed_task is not None and refreshed_document is not None
    assert refreshed_task.status == DocumentStatus.FAILED
    assert refreshed_document.status == DocumentStatus.FAILED
    assert "gpu proof missing" in (refreshed_task.error_message or "")
    verify.close()


def test_run_ingest_pipeline_completes_when_mineru_gpu_proof_artifact_passes_gate(tmp_path, monkeypatch):
    testing_session = setup_testing_db()
    monkeypatch.setattr(ingest_module, "SessionLocal", testing_session)
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "mineru"
    monkeypatch.setattr(ingest_module, "rag_service", rag_service)

    source_file = tmp_path / "questions.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    parsed_pdf = PDFParseResult(
        text="1. 如图所示，分析受力。\n[[asset:image-001]]\n【答案】A\n【解析】由受力分析可得。",
        assets=[
            ExtractedAsset(
                asset_id="image-001",
                filename="image-001.png",
                content_type="image/png",
                storage_path=str(tmp_path / "image-001.png"),
                public_url="/api/knowledge/documents/1/assets/image-001.png",
                title="受力图",
                description="figure-1",
            )
        ],
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="1. 如图所示，分析受力。"),
            PDFBlock(page_index=0, block_type="image", text="[[asset:image-001]]", asset_id="image-001"),
            PDFBlock(page_index=0, block_type="paragraph", text="【答案】A"),
            PDFBlock(page_index=0, block_type="paragraph", text="【解析】由受力分析可得。"),
        ],
        parser_provenance={"runtime_artifact": str(tmp_path / "tasks" / "1" / "mineru-runtime.json")},
    )

    def fake_extract_content(*args, **kwargs):
        return ExtractionResult(
            text=parsed_pdf.text,
            assets=parsed_pdf.assets,
            parsed_pdf=parsed_pdf,
        )

    monkeypatch.setattr(rag_service, "extract_content", fake_extract_content)

    session = testing_session()
    document = KnowledgeDocument(
        subject="物理",
        filename="questions.pdf",
        file_path=str(source_file),
        mime_type="application/pdf",
        size_bytes=source_file.stat().st_size,
        status=DocumentStatus.PENDING,
        resource_type="question_set",
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
    rows = verify.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)).all()
    assert refreshed_task is not None
    assert refreshed_task.status == DocumentStatus.COMPLETED
    assert rows
    assert rows[0].metadata_json.get("answer_text") == "A"
    assert rows[0].metadata_json.get("explanation_text") == "由受力分析可得。"
    assert rows[0].metadata_json.get("parser_provenance", {}).get("runtime_artifact")
    verify.close()


def test_run_ingest_pipeline_completion_message_flags_cpu_fallback(tmp_path, monkeypatch):
    testing_session = setup_testing_db()
    monkeypatch.setattr(ingest_module, "SessionLocal", testing_session)
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "mineru"
    monkeypatch.setattr(ingest_module, "rag_service", rag_service)

    source_file = tmp_path / "fallback.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    parsed_pdf = PDFParseResult(
        text="第一章 运动",
        blocks=[PDFBlock(page_index=0, block_type="paragraph", text="第一章 运动")],
        parser_provenance={
            "runtime_artifact": str(tmp_path / "tasks" / "2" / "mineru-runtime.json"),
            "requested_device": "cuda",
            "effective_device": "cpu",
            "device_fallback_reason": "cuda_unavailable",
        },
    )

    def fake_extract_content(*args, **kwargs):
        return ExtractionResult(text=parsed_pdf.text, parsed_pdf=parsed_pdf)

    monkeypatch.setattr(rag_service, "extract_content", fake_extract_content)

    session = testing_session()
    document = KnowledgeDocument(
        subject="物理",
        filename="fallback.pdf",
        file_path=str(source_file),
        mime_type="application/pdf",
        size_bytes=source_file.stat().st_size,
        status=DocumentStatus.PENDING,
        resource_type="textbook",
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
    assert refreshed_task is not None
    assert refreshed_task.status == DocumentStatus.COMPLETED
    assert "本次解析未使用 GPU，已回退 CPU" in (refreshed_task.error_message or "")
    verify.close()
