"""PDF_PARSER_BACKEND=auto 模式：本地优先、GPU/启动类失败自动降级远程 API。"""

from pathlib import Path
import zipfile

import pytest

from backend.config import Settings
from backend.services.embed_service import EmbedService
from backend.services.mineru_service import (
    MineruGpuRuntimeError,
    MineruMalformedOutputError,
    MineruStartupError,
)
from backend.services.pdf_parse_types import PDFBlock, PDFParseResult
from backend.services.rag_service import RagService
from backend.services.vector_store_service import VectorStoreService


def build_rag_service(tmp_path: Path) -> RagService:
    settings = Settings(
        CHROMADB_MODE="persistent",
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        CHROMADB_COLLECTION_PREFIX="studyagent-test",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        UPLOAD_PATH=str(tmp_path / "uploads"),
        EMBEDDING_MODEL_NAME="BAAI/bge-m3",
        EMBEDDING_BACKEND="hash",
        EMBEDDING_DEVICE="cpu",
        EMBEDDING_FALLBACK_TO_HASH=True,
        PDF_PARSER_BACKEND="auto",
    )
    embedder = EmbedService(settings)
    vector_store = VectorStoreService(settings, embedder)
    return RagService(settings=settings, embedder=embedder, vector_store=vector_store)


def make_pdf(tmp_path: Path) -> Path:
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    return source_file


def make_docx(tmp_path: Path) -> Path:
    docx_file = tmp_path / "questions.docx"
    with zipfile.ZipFile(docx_file, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>DOCX内容</w:t></w:r></w:p></w:body></w:document>""",
        )
    return docx_file


def parsed_result(text: str, *, provenance: dict | None = None, parser_backend: str = "pipeline") -> PDFParseResult:
    return PDFParseResult(
        text=text,
        blocks=[PDFBlock(page_index=0, block_type="paragraph", text=text)],
        parser_backend=parser_backend,
        parser_provenance=provenance or {},
    )


def healthy_snapshot() -> dict:
    return {
        "enabled": True,
        "runtime_ready": True,
        "mineru": {"installed": True},
        "configured_device": "cuda",
    }


def unhealthy_snapshot() -> dict:
    return {
        "enabled": True,
        "runtime_ready": False,
        "mineru": {"installed": False},
        "configured_device": "cuda",
    }


class ParseRecorder:
    """记录调用并可配置抛错的 parse_pdf 替身。"""

    def __init__(self, result: PDFParseResult | None = None, exc: Exception | None = None):
        self.result = result
        self.exc = exc
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, file_path: str, *, task_id: int, document_id: int, **kwargs) -> PDFParseResult:
        self.calls.append((file_path, task_id, document_id))
        if self.exc is not None:
            raise self.exc
        return self.result


def patch_backends(monkeypatch, *, local, remote, health):
    monkeypatch.setattr("backend.services.rag_service.mineru_service.health_snapshot", lambda: health)
    monkeypatch.setattr("backend.services.rag_service.mineru_service.parse_pdf", local)
    monkeypatch.setattr("backend.services.rag_service.mineru_remote_service.parse_pdf", remote)


def test_auto_healthy_local_uses_local(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    source_file = make_pdf(tmp_path)
    local = ParseRecorder(result=parsed_result("本地解析文本"))
    remote = ParseRecorder(result=parsed_result("远程不应被调用"))
    patch_backends(monkeypatch, local=local, remote=remote, health=healthy_snapshot())

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert local.calls == [(str(source_file), 9, 7)]
    assert remote.calls == []
    assert extracted.text == "本地解析文本"
    assert extracted.parser_provenance["auto_decision"] == "local"
    assert extracted.parser_provenance["auto_local_health"]["runtime_ready"] is True


def test_auto_unhealthy_local_goes_remote(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    source_file = make_pdf(tmp_path)
    local = ParseRecorder(result=parsed_result("本地不应被调用"))
    remote = ParseRecorder(result=parsed_result("远程解析文本", parser_backend="mineru-remote-official"))
    patch_backends(monkeypatch, local=local, remote=remote, health=unhealthy_snapshot())

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert local.calls == []
    assert remote.calls == [(str(source_file), 9, 7)]
    assert extracted.text == "远程解析文本"
    assert extracted.parser_provenance["auto_decision"] == "remote_no_local"


def test_auto_gpu_failure_falls_back_to_remote(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    source_file = make_pdf(tmp_path)
    local = ParseRecorder(exc=MineruGpuRuntimeError("no cuda gpus are available"))
    remote = ParseRecorder(result=parsed_result("降级远程文本", parser_backend="mineru-remote-official"))
    patch_backends(monkeypatch, local=local, remote=remote, health=healthy_snapshot())

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert local.calls == [(str(source_file), 9, 7)]
    assert remote.calls == [(str(source_file), 9, 7)]
    assert extracted.text == "降级远程文本"
    assert extracted.parser_provenance["auto_decision"] == "remote_fallback"
    assert "MineruGpuRuntimeError" in extracted.parser_provenance["auto_fallback_reason"]


def test_auto_gpu_failure_and_remote_unconfigured_raises_original_local_error(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    source_file = make_pdf(tmp_path)
    local = ParseRecorder(exc=MineruGpuRuntimeError("CUDA 运行时不可用"))
    remote = ParseRecorder(exc=MineruStartupError("MINERU_REMOTE_API_KEY is not configured"))
    patch_backends(monkeypatch, local=local, remote=remote, health=healthy_snapshot())

    with pytest.raises(MineruGpuRuntimeError) as exc_info:
        rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    message = str(exc_info.value)
    assert "CUDA 运行时不可用" in message
    assert "MINERU_REMOTE_API_KEY is not configured" in message


def test_auto_malformed_output_does_not_fall_back(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    source_file = make_pdf(tmp_path)
    local = ParseRecorder(exc=MineruMalformedOutputError("content_list_v2 missing"))
    remote = ParseRecorder(result=parsed_result("远程不应被调用"))
    patch_backends(monkeypatch, local=local, remote=remote, health=healthy_snapshot())

    with pytest.raises(MineruMalformedOutputError):
        rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert remote.calls == []


def test_auto_health_snapshot_cached_within_ttl_and_invalidated_on_fallback(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    source_file = make_pdf(tmp_path)
    health_calls: list[int] = []

    def fake_health() -> dict:
        health_calls.append(1)
        return healthy_snapshot()

    local = ParseRecorder(result=parsed_result("ok"))
    remote = ParseRecorder(result=parsed_result("ok"))
    monkeypatch.setattr("backend.services.rag_service.mineru_service.health_snapshot", fake_health)
    monkeypatch.setattr("backend.services.rag_service.mineru_service.parse_pdf", local)
    monkeypatch.setattr("backend.services.rag_service.mineru_remote_service.parse_pdf", remote)

    rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)
    rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)
    assert len(health_calls) == 1  # TTL 内复用缓存

    # 本地 GPU 失败触发降级 → 缓存立即失效，下次解析重新探测
    local.exc = MineruGpuRuntimeError("gpu lost")
    rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)
    assert len(health_calls) == 1  # 本次仍用 TTL 内缓存
    assert remote.calls  # 已降级远程
    rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)
    assert len(health_calls) == 2  # 缓存已失效，重新探测


def test_auto_question_docx_stays_on_local_mineru(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    docx_file = make_docx(tmp_path)
    local = ParseRecorder(result=parsed_result("1. 题干\n答案：A"))
    remote = ParseRecorder(result=parsed_result("远程不应被调用"))
    monkeypatch.setattr("backend.services.rag_service.mineru_service.parse_docx", local)
    monkeypatch.setattr("backend.services.rag_service.mineru_remote_service.parse_docx", remote)

    extracted = rag_service.extract_content(
        str(docx_file),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        document_id=7,
        task_id=9,
        resource_type="question_set",
    )

    assert local.calls == [(str(docx_file), 9, 7)]
    assert remote.calls == []
    assert "题干" in extracted.text


def test_explicit_backends_bypass_auto_decision(tmp_path, monkeypatch):
    source_file = make_pdf(tmp_path)

    # mineru_remote 显式选择：不做健康探测，直接走远程
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "mineru_remote"
    remote = ParseRecorder(result=parsed_result("远程文本", parser_backend="mineru-remote-official"))
    local = ParseRecorder(result=parsed_result("本地不应被调用"))
    monkeypatch.setattr("backend.services.rag_service.mineru_service.parse_pdf", local)
    monkeypatch.setattr("backend.services.rag_service.mineru_remote_service.parse_pdf", remote)
    monkeypatch.setattr(
        "backend.services.rag_service.mineru_service.health_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("health_snapshot should not run")),
    )

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)
    assert remote.calls == [(str(source_file), 9, 7)]
    assert local.calls == []
    assert "auto_decision" not in (extracted.parser_provenance or {})

    # legacy 显式选择：行为不变
    rag_service.settings.pdf_parser_backend = "legacy"
    monkeypatch.setattr(rag_service, "_extract_pdf_text", lambda _: "legacy pdf text")
    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)
    assert extracted.text == "legacy pdf text"
    assert extracted.parsed_pdf is None
