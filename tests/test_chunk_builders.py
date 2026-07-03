"""Unit tests for the chunk builders extracted from rag_service into backend/services/chunking/."""
from pathlib import Path
import zipfile

from backend.config import Settings
from backend.models.knowledge import KnowledgeDocument, ResourceType
from backend.services import rag_service as rag_service_module
from backend.services.chunking import (
    docx_chunk_builder,
    pdf_chunk_builder,
    question_chunk_builder,
    text_chunk_builder,
)
from backend.services.chunking.models import ExtractionResult, PreparedChunk
from backend.services.chunking.pdf_chunk_builder import PDFExtractionCandidate
from backend.services.embed_service import EmbedService
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
    )
    embedder = EmbedService(settings)
    vector_store = VectorStoreService(settings, embedder)
    return RagService(settings=settings, embedder=embedder, vector_store=vector_store)


# ---------------------------------------------------------------------------
# facade contract
# ---------------------------------------------------------------------------


def test_rag_service_inherits_all_chunk_builder_mixins():
    assert issubclass(RagService, text_chunk_builder.TextChunkBuilderMixin)
    assert issubclass(RagService, docx_chunk_builder.DocxChunkBuilderMixin)
    assert issubclass(RagService, pdf_chunk_builder.PdfChunkBuilderMixin)
    assert issubclass(RagService, question_chunk_builder.QuestionChunkBuilderMixin)


def test_facade_reexports_are_identical_objects():
    assert rag_service_module.PreparedChunk is PreparedChunk
    assert rag_service_module.ExtractionResult is ExtractionResult
    assert rag_service_module.PDFExtractionCandidate is PDFExtractionCandidate
    assert rag_service_module.QUESTION_START_PATTERN is question_chunk_builder.QUESTION_START_PATTERN
    assert (
        rag_service_module.QUESTION_SECTION_HEADING_PATTERN
        is question_chunk_builder.QUESTION_SECTION_HEADING_PATTERN
    )
    assert rag_service_module.PLAIN_TEXT_SPLIT_PATTERN is text_chunk_builder.PLAIN_TEXT_SPLIT_PATTERN
    assert rag_service_module.DOCX_WORD_NS is docx_chunk_builder.DOCX_WORD_NS
    assert (
        rag_service_module.LEGACY_QUESTION_DOCX_FORMULA_MESSAGE
        is docx_chunk_builder.LEGACY_QUESTION_DOCX_FORMULA_MESSAGE
    )


# ---------------------------------------------------------------------------
# text chunk builder
# ---------------------------------------------------------------------------


def test_text_builder_splits_long_plain_text_into_bounded_chunks(tmp_path):
    service = build_rag_service(tmp_path)
    text = "物理学是研究物质运动一般规律和物质基本结构的学科。" * 120

    chunks = service.split_text(text)

    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= service.settings.rag_chunk_size for chunk in chunks)


def test_text_builder_keeps_display_math_span_intact(tmp_path):
    service = build_rag_service(tmp_path)
    formula = "$$F=ma。其中F是合外力！m是质量？a是加速度$$"
    text = f"牛顿第二定律如下。{formula}以上是公式说明。"

    chunks = service.split_text(text)

    assert any(formula in chunk for chunk in chunks)


def test_text_builder_normalizes_markdown_soft_line_breaks(tmp_path):
    service = build_rag_service(tmp_path)
    text = "# 标题\n这一行\n被软换行拆开\n\n- 列表项保持独立\n```\ncode line\n```"

    normalized = service._normalize_markdown_text(text)

    assert "这一行 被软换行拆开" in normalized
    assert "- 列表项保持独立" in normalized
    assert "code line" in normalized


# ---------------------------------------------------------------------------
# docx chunk builder
# ---------------------------------------------------------------------------

DOCX_DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
    "<w:body>"
    "<w:p><w:r><w:t>第一段内容。</w:t></w:r></w:p>"
    "<w:p><m:oMath><m:r><m:t>E=mc^2</m:t></m:r></m:oMath></w:p>"
    "<w:tbl><w:tr>"
    "<w:tc><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>乙</w:t></w:r></w:p></w:tc>"
    "</w:tr></w:tbl>"
    "</w:body></w:document>"
)


def build_simple_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", DOCX_DOCUMENT_XML)


def test_docx_builder_extracts_paragraphs_math_and_tables(tmp_path):
    service = build_rag_service(tmp_path)
    docx_file = tmp_path / "simple.docx"
    build_simple_docx(docx_file)

    extracted = service._extract_docx_content(str(docx_file))

    assert isinstance(extracted, ExtractionResult)
    assert "第一段内容。" in extracted.text
    assert "$E=mc^2$" in extracted.text
    assert "甲 | 乙" in extracted.text
    assert extracted.assets == []


def test_docx_builder_normalize_block_text_collapses_whitespace(tmp_path):
    service = build_rag_service(tmp_path)

    normalized = service._normalize_docx_block_text("行一 \t\n\n\n 行二  ")

    assert normalized == "行一\n\n行二"


# ---------------------------------------------------------------------------
# pdf chunk builder
# ---------------------------------------------------------------------------


def test_pdf_builder_normalize_text_drops_noise_lines(tmp_path):
    service = build_rag_service(tmp_path)
    raw = "第一行内容\r\n~~~@\n\n\n第二行内容"

    normalized = service._normalize_pdf_text(raw)

    assert normalized == "第一行内容\n\n第二行内容"


def test_pdf_builder_prefers_cleaner_extraction_candidate(tmp_path):
    service = build_rag_service(tmp_path)
    clean_page = "力是改变物体运动状态的原因。加速度与合外力成正比、与质量成反比。"
    noisy_page = "力�是~~|改\\变物体运动状态的原因"

    assert service._score_extracted_text(clean_page) > service._score_extracted_text(noisy_page)

    candidates = [
        PDFExtractionCandidate(extractor="noisy", pages=[noisy_page]),
        PDFExtractionCandidate(extractor="clean", pages=[clean_page]),
    ]
    assert service._select_best_pdf_pages(candidates) == [clean_page]


def test_pdf_builder_detects_scanned_documents(tmp_path):
    service = build_rag_service(tmp_path)

    assert service._looks_like_scanned_pdf("", page_count=1) is True
    assert service._looks_like_scanned_pdf("有效正文" * 100, page_count=2) is False


# ---------------------------------------------------------------------------
# question chunk builder
# ---------------------------------------------------------------------------


def build_question_document(tmp_path: Path) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=901,
        subject="物理",
        filename="questions.docx",
        file_path=str(tmp_path / "questions.docx"),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=128,
        resource_type=ResourceType.QUESTION_SET.value,
    )


def test_question_builder_prepares_question_item_chunks(tmp_path):
    service = build_rag_service(tmp_path)
    document = build_question_document(tmp_path)
    text = "\n".join(
        [
            "一、单选题",
            "1. 下列关于加速度的说法正确的是（ ）",
            "A. 加速度大则速度一定大",
            "B. 加速度描述速度变化的快慢",
            "【答案】B",
            "【解析】加速度是速度的变化率。",
            "2. 关于位移与路程，下列说法正确的是（ ）",
            "A. 位移是矢量",
            "【答案】A",
        ]
    )

    prepared = service.prepare_document_chunks(document, text, source_format="docx")

    assert len(prepared) == 2
    assert all(isinstance(chunk, PreparedChunk) for chunk in prepared)
    first, second = prepared
    assert first.metadata["chunk_kind"] == "question_item"
    assert first.metadata["question_number"] == "1"
    assert first.metadata["answer_text"] == "B"
    assert "加速度是速度的变化率" in (first.metadata["explanation_text"] or "")
    assert "第1题" in first.content
    assert second.metadata["question_number"] == "2"
    assert second.metadata["answer_text"] == "A"


def test_question_builder_parses_numbered_blocks(tmp_path):
    service = build_rag_service(tmp_path)

    blocks = service._parse_numbered_blocks("1. 第一题题干\n2. 第二题题干")

    assert blocks == [("1", "第一题题干"), ("2", "第二题题干")]


def test_question_builder_composes_chunk_text(tmp_path):
    service = build_rag_service(tmp_path)

    composed = service._compose_question_chunk_text(
        number="3",
        question_text="题干内容",
        answer_text="C",
        explanation_text="因为如此",
    )

    assert composed == "第3题\n\n题目：\n题干内容\n\n答案：\nC\n\n解析：\n因为如此"
