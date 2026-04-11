from pathlib import Path
import base64
import zipfile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import Settings
from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.knowledge import KnowledgeChunk, KnowledgeDocument, ResourceType
from backend.services.embed_service import EmbedService
from backend.services.pdf_parse_types import ExtractedAsset, PDFBlock, PDFParseResult
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


def test_embedding_normalization_expands_common_latex_tokens():
    embedder = EmbedService(Settings(EMBEDDING_BACKEND="hash", EMBEDDING_DEVICE="cpu"))
    normalized = embedder._normalize_text_for_embedding(r"已知 \frac{a}{b}=c，求 x^2 和 v_0")

    assert "分式" in normalized
    assert "平方" in normalized
    assert "下标" in normalized


def test_split_text_returns_multiple_chunks_for_long_content(tmp_path):
    rag_service = build_rag_service(tmp_path)
    text = "函数单调性" * 200
    chunks = rag_service.split_text(text)
    assert len(chunks) >= 2


def test_split_text_keeps_display_formula_intact(tmp_path):
    rag_service = build_rag_service(tmp_path)
    text = ("位移分析。" * 90) + "\n$$s=v_0t+\\frac{1}{2}at^2$$\n" + ("受力分析。" * 90)
    chunks = rag_service.split_text(text)

    assert chunks
    assert sum(chunk.count("$$s=v_0t+\\frac{1}{2}at^2$$") for chunk in chunks) == 1
    assert all("$$s=v_0t+\\frac{1}{2}at^2" not in chunk or "$$s=v_0t+\\frac{1}{2}at^2$$" in chunk for chunk in chunks)


def test_retrieve_prefers_relevant_subject_chunk(tmp_path):
    rag_service = build_rag_service(tmp_path)
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        doc = KnowledgeDocument(
            subject="数学",
            filename="demo.txt",
            file_path="/tmp/demo.txt",
            mime_type="text/plain",
            size_bytes=12,
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)
        rag_service.ingest_document_text(session, doc, "函数单调性需要先看定义域，再比较自变量变化时函数值的变化。")
        result = rag_service.retrieve(session, "数学", "函数单调性第一步看什么")
        assert result.chunks
        assert "定义域" in result.context
    finally:
        session.close()


def test_retrieve_prefers_matching_grade_document(tmp_path):
    rag_service = build_rag_service(tmp_path)
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        grade1_doc = KnowledgeDocument(
            subject="物理",
            filename="grade1.txt",
            file_path="/tmp/grade1.txt",
            mime_type="text/plain",
            size_bytes=12,
            grade=1,
        )
        grade3_doc = KnowledgeDocument(
            subject="物理",
            filename="grade3.txt",
            file_path="/tmp/grade3.txt",
            mime_type="text/plain",
            size_bytes=12,
            grade=3,
        )
        session.add_all([grade1_doc, grade3_doc])
        session.commit()
        session.refresh(grade1_doc)
        session.refresh(grade3_doc)

        content = "牛顿第二定律描述合外力、质量和加速度之间的关系。"
        rag_service.ingest_document_text(session, grade1_doc, content)
        rag_service.ingest_document_text(session, grade3_doc, content)

        result = rag_service.retrieve(session, "物理", "牛顿第二定律是什么意思", student_grade=1)
        assert result.chunks
        assert result.chunks[0].document_id == grade1_doc.id
    finally:
        session.close()


def test_ingest_document_detects_chapter_heading_metadata(tmp_path):
    rag_service = build_rag_service(tmp_path)
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        doc = KnowledgeDocument(
            subject="物理",
            filename="textbook.txt",
            file_path="/tmp/textbook.txt",
            mime_type="text/plain",
            size_bytes=12,
            resource_type=ResourceType.TEXTBOOK.value,
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)

        rag_service.ingest_document_text(
            session,
            doc,
            "第一章 力\n力是物体间的相互作用。\n第二章 运动\n加速度描述速度变化快慢。",
        )
        rows = session.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == doc.id).order_by(KnowledgeChunk.chunk_index.asc()).all()
        assert rows
        assert rows[0].metadata_json.get("chapter") == "第一章 力"
        assert any(row.metadata_json.get("chapter") == "第二章 运动" for row in rows)
    finally:
        session.close()


def test_pdf_quality_score_prefers_cleaner_text(tmp_path):
    rag_service = build_rag_service(tmp_path)
    noisy = "物理教材目�录\n第二章 ~~ 机械振动"
    cleaner = "物理\n普通高中教科书\n第二章 机械振动"
    assert rag_service._score_extracted_text(cleaner) > rag_service._score_extracted_text(noisy)


def test_pdf_normalization_drops_short_noisy_lines(tmp_path):
    rag_service = build_rag_service(tmp_path)
    text = "qco~)魉易\n物理\n\n第二章 机械振动"
    normalized = rag_service._normalize_pdf_text(text)
    assert "qco~)魉易" not in normalized
    assert "第二章 机械振动" in normalized


def test_pdf_without_text_layer_returns_explicit_scan_error(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "legacy"
    source_file = tmp_path / "scan.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(rag_service, "_pdf_has_text_layer", lambda _: False)
    monkeypatch.setattr(rag_service, "_extract_pdf_candidates", lambda _: [])

    try:
        rag_service.extract_text(str(source_file), "application/pdf")
    except RuntimeError as exc:
        assert "无可用文本层" in str(exc)
    else:
        raise AssertionError("expected explicit scan error")


def test_extract_content_routes_pdf_to_mineru_when_pdf_parser_backend_is_mineru(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "mineru"
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    asset = ExtractedAsset(
        asset_id="image-001",
        filename="image-001.png",
        content_type="image/png",
        storage_path=str(tmp_path / "image-001.png"),
        public_url="/api/knowledge/documents/7/assets/image-001.png",
        title="示意图",
        description="images/example.png",
    )
    parsed_pdf = PDFParseResult(
        text="第1题\n\n题目：已知受力图。\n[[asset:image-001]]\n【答案】A\n【解析】由受力分析可得。",
        assets=[asset],
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="1. 已知受力图。"),
            PDFBlock(page_index=0, block_type="image", text="[[asset:image-001]]", asset_id="image-001"),
            PDFBlock(page_index=0, block_type="paragraph", text="【答案】A"),
            PDFBlock(page_index=0, block_type="paragraph", text="【解析】由受力分析可得。"),
        ],
    )

    calls: list[tuple[str, int, int]] = []

    def fake_parse(file_path: str, *, task_id: int, document_id: int):
        calls.append((file_path, task_id, document_id))
        return parsed_pdf

    monkeypatch.setattr("backend.services.rag_service.mineru_service.parse_pdf", fake_parse)
    monkeypatch.setattr(rag_service, "_extract_pdf_text", lambda _: (_ for _ in ()).throw(AssertionError("legacy path should not run")))

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert calls == [(str(source_file), 9, 7)]
    assert extracted.text == parsed_pdf.text
    assert extracted.parsed_pdf is parsed_pdf
    assert extracted.assets[0].asset_id == "image-001"


def test_extract_content_routes_pdf_to_legacy_when_pdf_parser_backend_is_legacy(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "legacy"
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(rag_service, "_extract_pdf_text", lambda _: "legacy pdf text")
    monkeypatch.setattr("backend.services.rag_service.mineru_service.parse_pdf", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mineru path should not run")))

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert extracted.text == "legacy pdf text"
    assert extracted.parsed_pdf is None


def test_extract_content_docx_and_txt_ignore_pdf_parser_backend(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    rag_service.settings.pdf_parser_backend = "mineru"

    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("文本内容", encoding="utf-8")
    assert rag_service.extract_content(str(txt_file), "text/plain").text == "文本内容"

    docx_file = tmp_path / "sample.docx"
    with zipfile.ZipFile(docx_file, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>DOCX内容</w:t></w:r></w:p></w:body></w:document>""",
        )
    extracted = rag_service.extract_content(str(docx_file), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "DOCX内容" in extracted.text


def test_pdf_bridge_preserves_answer_explanation_and_asset_refs_when_building_prepared_chunks(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=21,
        subject="物理",
        filename="questions.pdf",
        file_path=str(tmp_path / "questions.pdf"),
        mime_type="application/pdf",
        size_bytes=128,
        resource_type=ResourceType.QUESTION_SET.value,
    )
    parsed_pdf = PDFParseResult(
        text="1. 如图所示，分析受力。\n[[asset:image-001]]\n【答案】A\n【解析】由受力分析可得。",
        assets=[
            ExtractedAsset(
                asset_id="image-001",
                filename="image-001.png",
                content_type="image/png",
                storage_path=str(tmp_path / "image-001.png"),
                public_url="/api/knowledge/documents/21/assets/image-001.png",
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
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/9/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, assets=parsed_pdf.assets, parsed_pdf=parsed_pdf)

    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata["chunk_kind"] == "question_item"
    assert metadata["question_number"] == "1"
    assert metadata["answer_text"] == "A"
    assert metadata["explanation_text"] == "由受力分析可得。"
    assert metadata["parser_backend"] == "pipeline"
    assert metadata["parser_provenance"]["runtime_artifact"] == "data/tasks/9/mineru-runtime.json"
    assert metadata["source_format"] == "pdf"
    assert metadata["source_locator"] == "question:1|page:1"
    assert metadata["image_expectation"] == "required"
    assert metadata["image_binding_status"] == "bound"
    assert metadata["quality_flags"] == []
    assert metadata["question_uid"] == "qb:21:question:1|page:1"
    assert metadata["contains_images"] is True
    assert metadata["image_count"] == 1
    assert metadata["asset_refs"][0]["url"] == "/api/knowledge/documents/21/assets/image-001.png"
    assert "答案：" in chunks[0].content
    assert "解析：" in chunks[0].content


def test_pdf_bridge_uses_block_context_for_question_chunks_and_image_alignment(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=31,
        subject="物理",
        filename="structured-questions.pdf",
        file_path=str(tmp_path / "structured-questions.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.QUESTION_SET.value,
    )
    parsed_pdf = PDFParseResult(
        text=(
            "第一章 力与运动\n"
            "1.1 受力分析\n"
            "1. 如图所示，小球沿斜面下滑，求加速度。\n"
            "[[asset:image-001]]\n"
            "2. 已知 v-t 图像，求位移。\n"
            "参考答案\n"
            "1. 答案：a=g\\sin\\theta\n"
            "解析：由受力分析可得。\n"
            "2. 答案：位移等于图像与坐标轴围成的面积。"
        ),
        assets=[
            ExtractedAsset(
                asset_id="image-001",
                filename="image-001.png",
                content_type="image/png",
                storage_path=str(tmp_path / "image-001.png"),
                public_url="/api/knowledge/documents/31/assets/image-001.png",
                title="斜面示意图",
                description="figure-1",
            )
        ],
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="第一章 力与运动"),
            PDFBlock(page_index=0, block_type="title", text="1.1 受力分析"),
            PDFBlock(page_index=0, block_type="paragraph", text="1. 如图所示，小球沿斜面下滑，求加速度。"),
            PDFBlock(page_index=0, block_type="image", text="[[asset:image-001]]", asset_id="image-001"),
            PDFBlock(page_index=1, block_type="paragraph", text="2. 已知 v-t 图像，求位移。"),
            PDFBlock(page_index=1, block_type="paragraph", text="参考答案"),
            PDFBlock(page_index=1, block_type="paragraph", text="1. 答案：a=g\\sin\\theta"),
            PDFBlock(page_index=1, block_type="paragraph", text="解析：由受力分析可得。"),
            PDFBlock(page_index=1, block_type="paragraph", text="2. 答案：位移等于图像与坐标轴围成的面积。"),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/31/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, assets=parsed_pdf.assets, parsed_pdf=parsed_pdf)

    assert len(chunks) == 2

    first = chunks[0].metadata
    assert first["question_number"] == "1"
    assert first["chapter"] == "第一章 力与运动"
    assert first["section"] == "1.1 受力分析"
    assert first["tags"] == ["第一章 力与运动", "1.1 受力分析", "mineru-pdf"]
    assert first["contains_images"] is True
    assert first["image_count"] == 1
    assert first["asset_refs"][0]["url"] == "/api/knowledge/documents/31/assets/image-001.png"
    assert first["answer_text"] == "a=g\\sin\\theta"
    assert first["explanation_text"] == "由受力分析可得。"
    assert first["page_start"] == 1
    assert first["page_end"] == 1
    assert first["source_pages"] == [1]
    assert first["source_format"] == "pdf"
    assert first["source_locator"] == "question:1|page:1"
    assert first["question_uid"] == "qb:31:question:1|page:1"
    assert first["image_expectation"] == "required"
    assert first["image_binding_status"] == "bound"
    assert first["quality_flags"] == []

    second = chunks[1].metadata
    assert second["question_number"] == "2"
    assert second["chapter"] == "第一章 力与运动"
    assert second["section"] == "1.1 受力分析"
    assert second["contains_images"] is False
    assert second["answer_text"] == "位移等于图像与坐标轴围成的面积。"
    assert second["page_start"] == 2
    assert second["page_end"] == 2
    assert second["source_pages"] == [2]
    assert second["source_locator"] == "question:2|page:2"
    assert second["question_uid"] == "qb:31:question:2|page:2"
    assert second["image_expectation"] == "optional"
    assert second["image_binding_status"] == "optional_unbound"


def test_pdf_bridge_adds_structure_and_page_metadata_for_textbook_chunks(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=41,
        subject="物理",
        filename="textbook.pdf",
        file_path=str(tmp_path / "textbook.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=["教材"],
    )
    parsed_pdf = PDFParseResult(
        text="第一章 力与运动\n1.1 牛顿第一定律\n惯性描述物体保持原有运动状态的性质。",
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="第一章 力与运动"),
            PDFBlock(page_index=0, block_type="title", text="1.1 牛顿第一定律"),
            PDFBlock(page_index=1, block_type="paragraph", text="惯性描述物体保持原有运动状态的性质。"),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/41/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)

    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata["chapter"] == "第一章 力与运动"
    assert metadata["section"] == "1.1 牛顿第一定律"
    assert metadata["tags"] == ["教材", "第一章 力与运动", "1.1 牛顿第一定律", "mineru-pdf"]
    assert metadata["page_start"] == 2
    assert metadata["page_end"] == 2
    assert metadata["source_pages"] == [2]
    assert metadata["source_block_types"] == ["paragraph"]
    assert metadata["structure_path"] == ["第一章 力与运动", "1.1 牛顿第一定律"]


def test_pdf_bridge_recovers_textbook_structure_from_toc_pages(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=42,
        subject="物理",
        filename="toc-textbook.pdf",
        file_path=str(tmp_path / "toc-textbook.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=["教材"],
    )
    parsed_pdf = PDFParseResult(
        text=(
            "目录\n第一章运动的描述 ........ 3\n1.质点参考系 ........ 4\n"
            "第一章 运动的描述\n质点是用来描述物体大小可忽略的理想模型。\n"
            "参考系用于描述物体的位置和运动。"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="目录"),
            PDFBlock(page_index=0, block_type="paragraph", text="第一章运动的描述 ........ 3"),
            PDFBlock(page_index=0, block_type="paragraph", text="1.质点参考系 ........ 4"),
            PDFBlock(page_index=1, block_type="title", text="第一章 运动的描述"),
            PDFBlock(page_index=1, block_type="paragraph", text="质点是用来描述物体大小可忽略的理想模型。"),
            PDFBlock(page_index=2, block_type="paragraph", text="参考系用于描述物体的位置和运动。"),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/42/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)

    assert len(chunks) == 2
    second = chunks[1].metadata
    assert second["chapter"] == "第一章运动的描述"
    assert second["section"] == "1.质点参考系"
    assert second["structure_path"] == ["第一章运动的描述", "1.质点参考系"]
    assert second["structure_source"] == "toc_page_map"
    assert second["structure_confidence"] == "medium"
    assert second["page_start"] == 3
    assert second["page_end"] == 3
    assert second["source_pages"] == [3]
    assert second["retrieval_metadata"]["chapter_key"] == "第一章运动的描述"
    assert second["retrieval_metadata"]["section_key"] == "1质点参考系"
    assert second["diagnostic_metadata"]["parser_backend"] == "pipeline"
    assert second["ingestion_metadata"]["toc_page_offset"] == -1
    assert second["page_start"] == 3
    assert second["page_end"] == 3
    assert second["source_pages"] == [3]


def test_extract_heading_context_supports_compact_textbook_titles_and_single_level_sections(tmp_path):
    rag_service = build_rag_service(tmp_path)

    chapter = rag_service._extract_heading_context("第一章运动的描述", ResourceType.TEXTBOOK.value)
    section = rag_service._extract_heading_context("1.质点参考系", ResourceType.TEXTBOOK.value)
    textbook_section = rag_service._extract_heading_context("第三节 加速度和力的关系", ResourceType.TEXTBOOK.value)

    assert chapter == {"chapter": "第一章运动的描述", "section": None}
    assert section == {"chapter": None, "section": "1.质点参考系"}
    assert textbook_section == {"chapter": None, "section": "第三节 加速度和力的关系"}


def test_pdf_bridge_keeps_textbook_chapter_anchor_for_section_titles(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=57,
        subject="物理",
        filename="textbook-chapter-section.pdf",
        file_path=str(tmp_path / "textbook-chapter-section.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=["教材"],
    )
    parsed_pdf = PDFParseResult(
        text=(
            "目录\n第三章 运动定律 ........ 74\n第三节 加速度和力的关系 ........ 79\n"
            "第三节 加速度和力的关系\n质量一定时加速度和力的关系是怎样的呢？"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="目录"),
            PDFBlock(page_index=0, block_type="paragraph", text="第三章 运动定律 ........ 74"),
            PDFBlock(page_index=0, block_type="paragraph", text="第三节 加速度和力的关系 ........ 79"),
            PDFBlock(page_index=78, block_type="title", text="第三节 加速度和力的关系"),
            PDFBlock(page_index=78, block_type="paragraph", text="质量一定时加速度和力的关系是怎样的呢？"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)

    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata["chapter"] == "第三章 运动定律"
    assert metadata["section"] == "第三节 加速度和力的关系"
    assert metadata["structure_path"] == ["第三章 运动定律", "第三节 加速度和力的关系"]


def test_pdf_bridge_uses_page_header_chapter_context_before_late_page_header_block(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=58,
        subject="物理",
        filename="page-header-chapter.pdf",
        file_path=str(tmp_path / "page-header-chapter.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=["教材"],
    )
    parsed_pdf = PDFParseResult(
        text="第三节 加速度和力的关系\n质量一定时加速度和力的关系是怎样的呢？\n第三章 运动定律",
        blocks=[
            PDFBlock(page_index=79, block_type="title", text="第三节 加速度和力的关系"),
            PDFBlock(page_index=79, block_type="paragraph", text="质量一定时加速度和力的关系是怎样的呢？"),
            PDFBlock(page_index=79, block_type="page_header", text="第三章 运动定律"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)

    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata["chapter"] == "第三章 运动定律"
    assert metadata["section"] == "第三节 加速度和力的关系"
    assert metadata["structure_source"] in {"page_header", "body_heading"}


def test_pdf_bridge_clears_stale_section_when_page_header_switches_chapter(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=61,
        subject="物理",
        filename="page-header-chapter-switch.pdf",
        file_path=str(tmp_path / "page-header-chapter-switch.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=["教材"],
    )
    parsed_pdf = PDFParseResult(
        text=(
            "目录\n第三章 运动定律 ........ 79\n第二节 旧章节 ........ 80\n第四章 曲线运动 ........ 81\n"
            "第二节 旧章节\n上一页正文\n第四章 曲线运动\n本页正文"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="目录"),
            PDFBlock(page_index=0, block_type="paragraph", text="第三章 运动定律 ........ 79"),
            PDFBlock(page_index=0, block_type="paragraph", text="第二节 旧章节 ........ 80"),
            PDFBlock(page_index=0, block_type="paragraph", text="第四章 曲线运动 ........ 81"),
            PDFBlock(page_index=79, block_type="title", text="第二节 旧章节"),
            PDFBlock(page_index=79, block_type="paragraph", text="上一页正文"),
            PDFBlock(page_index=80, block_type="page_header", text="第四章 曲线运动"),
            PDFBlock(page_index=80, block_type="paragraph", text="本页正文"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    switched_chunk = next(chunk for chunk in chunks if "本页正文" in chunk.content)

    assert switched_chunk.metadata["chapter"] == "第四章 曲线运动"
    assert switched_chunk.metadata["section"] is None
    assert switched_chunk.metadata["structure_source"] in {"page_header", "body_heading"}


def test_extract_heading_context_rejects_sentence_like_textbook_steps(tmp_path):
    rag_service = build_rag_service(tmp_path)

    assert rag_service._extract_heading_context("（2）“您这么早就来啦，抱歉！让您等了这么久。”", ResourceType.TEXTBOOK.value) == {
        "chapter": None,
        "section": None,
    }
    assert rag_service._extract_heading_context("（3）汽车在哪段时间驶离出发点，在哪段", ResourceType.TEXTBOOK.value) == {
        "chapter": None,
        "section": None,
    }
    assert rag_service._extract_heading_context("2.测量各计数点到起始点0的距离x，记录在表1中；", ResourceType.TEXTBOOK.value) == {
        "chapter": None,
        "section": None,
    }
    assert rag_service._extract_heading_context("4.根据 ", ResourceType.TEXTBOOK.value) == {
        "chapter": None,
        "section": None,
    }


def test_pdf_bridge_maps_back_matter_pages_to_back_matter_headings(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=43,
        subject="物理",
        filename="back-matter.pdf",
        file_path=str(tmp_path / "back-matter.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
        tags_json=["教材"],
    )
    parsed_pdf = PDFParseResult(
        text=(
            "目录\n"
            "6.超重和失重 ........ 109\n"
            "课题研究 ........ 116\n"
            "学生实验 ........ 121\n"
            "索引 ........ 125\n"
            "超重和失重\n"
            "课题研究\n"
            "请同学们根据兴趣自行选择研究课题。\n"
            "学生实验\n"
            "实验在中学物理中占有非常重要的地位。\n"
            "索引\n"
            "超重 110\n"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="目录"),
            PDFBlock(page_index=0, block_type="paragraph", text="6.超重和失重 ........ 109"),
            PDFBlock(page_index=0, block_type="paragraph", text="课题研究 ........ 116"),
            PDFBlock(page_index=0, block_type="paragraph", text="学生实验 ........ 121"),
            PDFBlock(page_index=0, block_type="paragraph", text="索引 ........ 125"),
            PDFBlock(page_index=1, block_type="title", text="超重和失重"),
            PDFBlock(page_index=7, block_type="title", text="课题研究"),
            PDFBlock(page_index=7, block_type="paragraph", text="请同学们根据兴趣自行选择研究课题。"),
            PDFBlock(page_index=12, block_type="title", text="学生实验"),
            PDFBlock(page_index=12, block_type="paragraph", text="实验在中学物理中占有非常重要的地位。"),
            PDFBlock(page_index=16, block_type="title", text="索引"),
            PDFBlock(page_index=16, block_type="paragraph", text="超重 110"),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/43/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)

    research_chunk = next(chunk for chunk in chunks if "自行选择研究课题" in chunk.content)
    experiment_chunk = next(chunk for chunk in chunks if "实验在中学物理中占有非常重要的地位" in chunk.content)
    index_chunk = next(chunk for chunk in chunks if "超重 110" in chunk.content)

    assert research_chunk.metadata["chapter"] == "课题研究"
    assert research_chunk.metadata["section"] is None
    assert research_chunk.metadata["structure_source"] == "body_heading"
    assert experiment_chunk.metadata["chapter"] == "学生实验"
    assert experiment_chunk.metadata["section"] is None
    assert index_chunk.metadata["chapter"] == "索引"
    assert index_chunk.metadata["section"] is None


def test_pdf_bridge_normalizes_html_table_markup_and_suppresses_repeated_footer_noise(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=44,
        subject="物理",
        filename="table-noise.pdf",
        file_path=str(tmp_path / "table-noise.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    table_markup = (
        "<table><tr><td rowspan=1 colspan=1>传感器名称</td><td rowspan=1 colspan=1>输入的物理量</td>"
        "<td rowspan=1 colspan=1>输出的物理量</td></tr>"
        "<tr><td rowspan=1 colspan=1>光敏电阻</td><td rowspan=1 colspan=1>光照强度</td>"
        "<td rowspan=1 colspan=1>电阻变化</td></tr></table>"
    )
    parsed_pdf = PDFParseResult(
        text=(
            "第一章 传感器\n"
            f"{table_markup}\n"
            "人民教育出版社\n"
            "表格说明\n"
            "人民教育出版社\n"
            "热敏电阻用于测温。\n"
            "人民教育出版社\n"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="title", text="第一章 传感器"),
            PDFBlock(page_index=0, block_type="paragraph", text=table_markup, metadata={"content_roles": ["table_body"]}),
            PDFBlock(page_index=0, block_type="paragraph", text="人民教育出版社", metadata={"content_roles": ["page_footer_content"]}),
            PDFBlock(page_index=1, block_type="paragraph", text="表格说明", metadata={"content_roles": ["table_caption"]}),
            PDFBlock(page_index=1, block_type="paragraph", text="人民教育出版社", metadata={"content_roles": ["page_footer_content"]}),
            PDFBlock(page_index=2, block_type="paragraph", text="热敏电阻用于测温。"),
            PDFBlock(page_index=2, block_type="paragraph", text="人民教育出版社", metadata={"content_roles": ["page_footer_content"]}),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/44/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "<table" not in combined
    assert "<td" not in combined
    assert "传感器名称 | 输入的物理量 | 输出的物理量" in combined
    assert "光敏电阻 | 光照强度 | 电阻变化" in combined
    assert "人民教育出版社" not in combined
    assert "表格说明" in combined
    assert any(chunk.metadata["chapter"] == "第一章 传感器" for chunk in chunks)


def test_pdf_bridge_keeps_repeated_text_when_not_at_page_edge(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=45,
        subject="物理",
        filename="middle-repeat.pdf",
        file_path=str(tmp_path / "middle-repeat.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    repeated = "人民教育出版社"
    parsed_pdf = PDFParseResult(
        text="".join(
            f"第一页起始\n甲内容\n{repeated}\n乙内容\n第一页结尾\n"
            f"第二页起始\n丙内容\n{repeated}\n丁内容\n第二页结尾\n"
            f"第三页起始\n戊内容\n{repeated}\n己内容\n第三页结尾\n"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="第一页起始"),
            PDFBlock(page_index=0, block_type="paragraph", text="甲内容"),
            PDFBlock(page_index=0, block_type="paragraph", text=repeated),
            PDFBlock(page_index=0, block_type="paragraph", text="乙内容"),
            PDFBlock(page_index=0, block_type="paragraph", text="第一页结尾"),
            PDFBlock(page_index=1, block_type="paragraph", text="第二页起始"),
            PDFBlock(page_index=1, block_type="paragraph", text="丙内容"),
            PDFBlock(page_index=1, block_type="paragraph", text=repeated),
            PDFBlock(page_index=1, block_type="paragraph", text="丁内容"),
            PDFBlock(page_index=1, block_type="paragraph", text="第二页结尾"),
            PDFBlock(page_index=2, block_type="paragraph", text="第三页起始"),
            PDFBlock(page_index=2, block_type="paragraph", text="戊内容"),
            PDFBlock(page_index=2, block_type="paragraph", text=repeated),
            PDFBlock(page_index=2, block_type="paragraph", text="己内容"),
            PDFBlock(page_index=2, block_type="paragraph", text="第三页结尾"),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/45/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "人民教育出版社" in combined


def test_pdf_bridge_preserves_uncertain_footer_when_frequency_is_insufficient(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=46,
        subject="物理",
        filename="uncertain-footer.pdf",
        file_path=str(tmp_path / "uncertain-footer.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    repeated = "人民教育出版社"
    parsed_pdf = PDFParseResult(
        text="".join(
            f"第一页正文\n{repeated}\n"
            f"第二页正文\n{repeated}\n"
            "第三页正文\n不同页脚\n"
            "第四页正文\n另一个页脚\n"
        ),
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="第一页正文"),
            PDFBlock(page_index=0, block_type="paragraph", text=repeated, metadata={"content_roles": ["page_footer_content"]}),
            PDFBlock(page_index=1, block_type="paragraph", text="第二页正文"),
            PDFBlock(page_index=1, block_type="paragraph", text=repeated, metadata={"content_roles": ["page_footer_content"]}),
            PDFBlock(page_index=2, block_type="paragraph", text="第三页正文"),
            PDFBlock(page_index=2, block_type="paragraph", text="不同页脚", metadata={"content_roles": ["page_footer_content"]}),
            PDFBlock(page_index=3, block_type="paragraph", text="第四页正文"),
            PDFBlock(page_index=3, block_type="paragraph", text="另一个页脚", metadata={"content_roles": ["page_footer_content"]}),
        ],
        parser_backend="pipeline",
        parser_provenance={"runtime_artifact": "data/tasks/46/mineru-runtime.json"},
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "人民教育出版社" in combined


def test_pdf_bridge_normalizes_equation_inline_into_renderable_latex(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=47,
        subject="物理",
        filename="formula-inline.pdf",
        file_path=str(tmp_path / "formula-inline.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="霍尔电压满足\n\nequation_inline\nU _ { \\mathrm { H } } = { \\frac { B I } { n e d } }",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="霍尔电压满足"),
            PDFBlock(page_index=0, block_type="paragraph", text="equation_inline\nU _ { \\mathrm { H } } = { \\frac { B I } { n e d } }"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" not in combined
    assert "$U_{\\mathrm{H}}" in combined
    assert "\\frac{" in combined


def test_pdf_bridge_repairs_split_symbolic_label_lines_for_chunk_text(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=66,
        subject="物理",
        filename="split-labels.pdf",
        file_path=str(tmp_path / "split-labels.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text=(
            "2．图1.37表示用平行四边形法则求三个共点力\n"
            "$F_{1}$\n"
            "$F_{2}$\n"
            "$F_{3}$\n"
            "的合力F．先求出\n"
            "$F_{1}$\n"
            "和\n"
            "$F_{2}$\n"
            "的合力，再求出这个合力与\n"
            "$F_{3}$\n"
            "的合力F．"
        ),
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text=(
                    "2．图1.37表示用平行四边形法则求三个共点力\n"
                    "$F_{1}$\n"
                    "$F_{2}$\n"
                    "$F_{3}$\n"
                    "的合力F．先求出\n"
                    "$F_{1}$\n"
                    "和\n"
                    "$F_{2}$\n"
                    "的合力，再求出这个合力与\n"
                    "$F_{3}$\n"
                    "的合力F．"
                ),
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "$F_{1}$ $F_{2}$ $F_{3}$ 的合力F．先求出 $F_{1}$ 和 $F_{2}$ 的合力" in combined
    assert "\n$F_{1}$\n$F_{2}$\n$F_{3}$\n" not in f"\n{combined}\n"
    assert "\n$F_{1}$\n和\n$F_{2}$\n" not in f"\n{combined}\n"


def test_pdf_bridge_keeps_non_index_symbol_lines_split_when_pattern_is_weak(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=67,
        subject="物理",
        filename="plain-symbol-lines.pdf",
        file_path=str(tmp_path / "plain-symbol-lines.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="如图所示，作图时依次标出\nA\nB\nC\n三点，再连接成线段。",
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text="如图所示，作图时依次标出\nA\nB\nC\n三点，再连接成线段。",
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "\nA\nB\nC\n" in f"\n{combined}\n"
    assert "如图所示，作图时依次标出 A B C 三点" not in combined


def test_pdf_bridge_keeps_single_symbolic_label_line_when_no_continuation_exists(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=68,
        subject="物理",
        filename="single-symbolic-label.pdf",
        file_path=str(tmp_path / "single-symbolic-label.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="设未知量为\n$F_{1}$",
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text="设未知量为\n$F_{1}$",
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert combined.endswith("设未知量为\n$F_{1}$")
    assert "设未知量为 $F_{1}$" not in combined


def test_pdf_bridge_inlines_standalone_symbol_and_short_formula_between_prose_lines(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=69,
        subject="物理",
        filename="inline-symbol-formula.pdf",
        file_path=str(tmp_path / "inline-symbol-formula.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text=(
            "合力的方向可以用合力跟原来任一个力的夹角表示出来．图中用\n"
            "F\n"
            "跟 $F_{1}$ 的夹角\n"
            "$\\phi$\n"
            "来表示．钢索NO与水平悬臂 MO 成\n"
            "$30^{\\circ}$\n"
            "角，当起重机吊着\n"
            "$4.0 \\times 10^{4}$\n"
            "牛的货物时。"
        ),
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text=(
                    "合力的方向可以用合力跟原来任一个力的夹角表示出来．图中用\n"
                    "F\n"
                    "跟 $F_{1}$ 的夹角\n"
                    "$\\phi$\n"
                    "来表示．钢索NO与水平悬臂 MO 成\n"
                    "$30^{\\circ}$\n"
                    "角，当起重机吊着\n"
                    "$4.0 \\times 10^{4}$\n"
                    "牛的货物时。"
                ),
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "图中用 F 跟 $F_{1}$ 的夹角 $\\phi$ 来表示" in combined
    assert "成 $30^{\\circ}$ 角，当起重机吊着 $4.0 \\times 10^{4}$ 牛的货物时" in combined
    assert "\nF\n" not in f"\n{combined}\n"
    assert "\n$\\phi$\n" not in f"\n{combined}\n"
    assert "\n$30^{\\circ}$\n" not in f"\n{combined}\n"
    assert "\n$4.0 \\times 10^{4}$\n" not in f"\n{combined}\n"


def test_pdf_bridge_removes_sparse_array_empty_row_variant(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=70,
        subject="物理",
        filename="array-empty-row-variant.pdf",
        file_path=str(tmp_path / "array-empty-row-variant.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="\\begin{array}{r}{F_{1} = G \\sin \\theta ,} \\\\ {\\} \\\\ {F_{2} = G \\cos \\theta .} \\end{array}",
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text="\\begin{array}{r}{F_{1} = G \\sin \\theta ,} \\\\ {\\} \\\\ {F_{2} = G \\cos \\theta .} \\end{array}",
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "{\\}" not in combined
    assert "$$\\begin{array}{r}" in combined
    assert "F_{1} = G \\sin \\theta" in combined
    assert "F_{2} = G \\cos \\theta" in combined


def test_pdf_bridge_normalizes_simple_table_rows_without_leaking_control_markers(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=59,
        subject="物理",
        filename="formula-simple-table.pdf",
        file_path=str(tmp_path / "formula-simple-table.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text=(
            "<table><tr><td>F</td><td>a</td></tr>"
            "<tr><td>(kgf)</td><td> $\\left( \\mathrm { m } / \\mathrm { s } ^ { 2 } \\right)$ </td></tr>"
            "<tr><td>0.020</td><td>0.193</td></tr></table>\n"
            "simple_table\n1"
        ),
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="table",
                text=(
                    "<table><tr><td>F</td><td>a</td></tr>"
                    "<tr><td>(kgf)</td><td> $\\left( \\mathrm { m } / \\mathrm { s } ^ { 2 } \\right)$ </td></tr>"
                    "<tr><td>0.020</td><td>0.193</td></tr></table>\n"
                    "simple_table\n1"
                ),
                metadata={"table_type": "simple_table"},
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "simple_table" not in combined
    assert "\n1\n" not in f"\n{combined}\n"
    assert "F | a" in combined
    assert "(kgf) | $" in combined
    assert "0.020 | 0.193" in combined
    assert "$0.020 | 0.193$" not in combined


def test_pdf_bridge_repairs_sparse_simple_table_rows_and_mixed_formula_headers(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=64,
        subject="物理",
        filename="sparse-simple-table.pdf",
        file_path=str(tmp_path / "sparse-simple-table.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text=(
            "<table><tr><td>汽车型号</td><td>初速度  $v _ { 0 }$   $\\left( \\mathrm { { k m } / h } \\right)$ </td>"
            "<td>末速度  $v _ { t }$   $\\left( \\mathrm { { k m } / h } \\right)$ </td>"
            "<td>时间t加速度α (s)  $\\left( \\mathrm { m } / \\mathrm { s } ^ { 2 } \\right)$ </td></tr>"
            "<tr><td>某型号高级轿车</td><td>20</td><td>50 7</td><td></td></tr></table>"
        ),
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="table",
                text=(
                    "<table><tr><td>汽车型号</td><td>初速度  $v _ { 0 }$   $\\left( \\mathrm { { k m } / h } \\right)$ </td>"
                    "<td>末速度  $v _ { t }$   $\\left( \\mathrm { { k m } / h } \\right)$ </td>"
                    "<td>时间t加速度α (s)  $\\left( \\mathrm { m } / \\mathrm { s } ^ { 2 } \\right)$ </td></tr>"
                    "<tr><td>某型号高级轿车</td><td>20</td><td>50 7</td><td></td></tr></table>"
                ),
                metadata={"table_type": "simple_table"},
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "汽车型号 | 初速度 $v_{0}$ $\\left( \\mathrm{km/h} \\right)$" in combined
    assert "末速度 $v_{t}$ $\\left( \\mathrm{km/h} \\right)$" in combined
    assert "某型号高级轿车 | 20 | 50 | 7" in combined


def test_pdf_bridge_does_not_treat_conditional_probability_as_pseudo_table(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=62,
        subject="数学",
        filename="probability.pdf",
        file_path=str(tmp_path / "probability.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text=r"P(A|B) = \frac{P(A\cap B)}{P(B)}",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text=r"P(A|B) = \frac{P(A\cap B)}{P(B)}"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert r"P(A|B)" in combined
    assert "P(A |" not in combined


def test_pdf_bridge_does_not_treat_absolute_value_as_pseudo_table(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=63,
        subject="数学",
        filename="absolute-value.pdf",
        file_path=str(tmp_path / "absolute-value.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="|a|=1",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="|a|=1"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "|a|=1" in combined
    assert "a |" not in combined


def test_pdf_bridge_wraps_equation_inline_short_formula_expression(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=65,
        subject="物理",
        filename="short-formula-expression.pdf",
        file_path=str(tmp_path / "short-formula-expression.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="equation_inline\nG + m a",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="equation_inline\nG + m a"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" not in combined
    assert "$G + m a$" in combined


def test_pdf_bridge_removes_empty_array_row_artifacts(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=66,
        subject="物理",
        filename="array-empty-row.pdf",
        file_path=str(tmp_path / "array-empty-row.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="\\begin{array}{l}{{v_{t} = v_{0} - g t}} \\\\ {{\\}} \\\\ {{s = v_{0} t - {\\frac{1}{2}} g t^{2}}} \\end{array}",
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text="\\begin{array}{l}{{v_{t} = v_{0} - g t}} \\\\ {{\\}} \\\\ {{s = v_{0} t - {\\frac{1}{2}} g t^{2}}} \\end{array}",
            )
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "{{\\}}" not in combined
    assert "$$\\begin{array}{l}" in combined
    assert "s = v_{0} t - {\\frac{1}{2}} g t^{2}" in combined


def test_pdf_bridge_balances_unclosed_parenthesized_unit_formula(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=67,
        subject="物理",
        filename="unit-balance.pdf",
        file_path=str(tmp_path / "unit-balance.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="equation_inline\n\\mathrm { ( k g \\cdot m / s ^ { 2 } }",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="equation_inline\n\\mathrm { ( k g \\cdot m / s ^ { 2 } }"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "$\\mathrm{(kg\\cdotm/s^{2})}$" in combined


def test_pdf_bridge_wraps_raw_latex_without_existing_delimiters(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=51,
        subject="物理",
        filename="formula-raw-latex.pdf",
        file_path=str(tmp_path / "formula-raw-latex.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="加速度关系\n\\frac { v } { t } = a",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="加速度关系"),
            PDFBlock(page_index=0, block_type="paragraph", text="\\frac { v } { t } = a"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "$$\\frac{v}{t} = a$$" in combined


def test_pdf_bridge_normalizes_equation_inline_degree_payload(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=53,
        subject="物理",
        filename="formula-degree.pdf",
        file_path=str(tmp_path / "formula-degree.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="v与x轴成\nequation_inline\n6 0 ^ { \\circ }\n角",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="v与x轴成"),
            PDFBlock(page_index=0, block_type="paragraph", text="equation_inline\n6 0 ^ { \\circ }\n角"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" not in combined
    assert "$60^{\\circ}$" in combined


def test_pdf_bridge_wraps_equation_inline_greek_symbol_payload(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=54,
        subject="物理",
        filename="formula-greek.pdf",
        file_path=str(tmp_path / "formula-greek.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="磁通量的变化量为\nequation_inline\n\\Delta \\phi\n。",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="磁通量的变化量为"),
            PDFBlock(page_index=0, block_type="paragraph", text="equation_inline\n\\Delta \\phi\n。"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" not in combined
    assert "$\\Delta \\phi$" in combined


def test_pdf_bridge_removes_latex_marker_and_image_path_after_formula_normalization(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=48,
        subject="物理",
        filename="formula-image-pair.pdf",
        file_path=str(tmp_path / "formula-image-pair.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="F = q v B\nlatex\nimages/demo-formula.jpg",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="F = q v B"),
            PDFBlock(page_index=0, block_type="paragraph", text="latex"),
            PDFBlock(page_index=0, block_type="paragraph", text="images/demo-formula.jpg"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "latex" not in combined
    assert "images/demo-formula.jpg" not in combined
    assert "$F = q v B$" in combined


def test_pdf_bridge_normalizes_scientific_notation_in_formula_segments(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=49,
        subject="物理",
        filename="formula-scientific.pdf",
        file_path=str(tmp_path / "formula-scientific.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="equation_inline\n1 . 6 7 \\times 1 0 ^ { - 2 7 } ~ \\mathrm { k g }",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="equation_inline\n1 . 6 7 \\times 1 0 ^ { - 2 7 } ~ \\mathrm { k g }"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "$1.67 \\times 10^{-27} ~ \\mathrm{kg}$" in combined


def test_pdf_bridge_normalizes_array_environments_into_renderable_display_math(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=52,
        subject="物理",
        filename="formula-array.pdf",
        file_path=str(tmp_path / "formula-array.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="\\begin { array } { c } F = m a \\\\ p = m v \\end { array }",
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="paragraph",
                text="\\begin { array } { c } F = m a \\\\ p = m v \\end { array }",
            ),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "$$\\begin{array}{c}" in combined
    assert "\\end{array}$$" in combined


def test_pdf_bridge_removes_bare_image_path_when_asset_is_already_bound(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=55,
        subject="物理",
        filename="asset-path-noise.pdf",
        file_path=str(tmp_path / "asset-path-noise.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="[[asset:image-001]]\nimages/demo-formula.jpg\n图1.3-2洛伦兹力演示仪示意图",
        assets=[
            ExtractedAsset(
                asset_id="image-001",
                filename="image-001.jpg",
                content_type="image/jpeg",
                storage_path=str(tmp_path / "image-001.jpg"),
                public_url="/api/knowledge/documents/55/assets/image-001.jpg",
                title="demo-formula",
                description="images/demo-formula.jpg",
            )
        ],
        blocks=[
            PDFBlock(
                page_index=0,
                block_type="image",
                text="[[asset:image-001]]\nimages/demo-formula.jpg\n图1.3-2洛伦兹力演示仪示意图",
                asset_id="image-001",
            ),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, assets=parsed_pdf.assets, parsed_pdf=parsed_pdf)
    assert len(chunks) == 1
    combined = chunks[0].content

    assert "images/demo-formula.jpg" not in combined
    assert "【附图1：demo-formula】" in combined
    assert chunks[0].metadata["contains_images"] is True
    assert chunks[0].metadata["image_count"] == 1


def test_pdf_bridge_drops_equation_inline_marker_for_symbolic_segment_labels(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=56,
        subject="物理",
        filename="formula-symbolic-label.pdf",
        file_path=str(tmp_path / "formula-symbolic-label.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="通过\nequation_inline\na P\n段电阻丝的电流是多大？",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="通过\nequation_inline\na P\n段电阻丝的电流是多大？"),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" not in combined
    assert "通过\na P 段电阻丝的电流是多大？" in combined
    assert "$a P$" not in combined


def test_pdf_bridge_drops_equation_inline_marker_for_axis_style_labels(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=60,
        subject="物理",
        filename="formula-axis-label.pdf",
        file_path=str(tmp_path / "formula-axis-label.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    parsed_pdf = PDFParseResult(
        text="根据实验数据作出\nequation_inline\na - 1 / m\n图象.",
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text="根据实验数据作出\nequation_inline\na - 1 / m\n图象."),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" not in combined
    assert "根据实验数据作出\na - 1 / m 图象." in combined
    assert "$a - 1 / m$" not in combined


def test_pdf_bridge_preserves_uncertain_formula_like_text_when_signal_is_weak(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=50,
        subject="物理",
        filename="formula-uncertain.pdf",
        file_path=str(tmp_path / "formula-uncertain.pdf"),
        mime_type="application/pdf",
        size_bytes=256,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    text = "记号说明：equation_inline 表示系统内部标记，不是公式本身。"
    parsed_pdf = PDFParseResult(
        text=text,
        blocks=[
            PDFBlock(page_index=0, block_type="paragraph", text=text),
        ],
        parser_backend="pipeline",
    )

    chunks = rag_service.prepare_document_chunks(document, parsed_pdf.text, parsed_pdf=parsed_pdf)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "equation_inline" in combined
    assert "$" not in combined


def test_extract_heading_context_skips_question_like_lines_for_textbook_chunks(tmp_path):
    rag_service = build_rag_service(tmp_path)

    result = rag_service._extract_heading_context("(2）当n =9，l=10.0 cm时，磁感应强度是多少？", ResourceType.TEXTBOOK.value)
    result_with_prompt = rag_service._extract_heading_context("2.写出这段长为ut的导线所受的安培力", ResourceType.TEXTBOOK.value)

    assert result == {"chapter": None, "section": None}
    assert result_with_prompt == {"chapter": None, "section": None}


def test_rerank_rows_prefers_chunk_level_structure_match_over_wrong_chapter(tmp_path):
    rag_service = build_rag_service(tmp_path)
    question = "第一章运动的描述里，1.质点参考系的定义是什么？"
    profile = rag_service._infer_question_profile(question)

    wrong_document = KnowledgeDocument(
        id=101,
        subject="物理",
        filename="wrong.pdf",
        file_path="/tmp/wrong.pdf",
        mime_type="application/pdf",
        size_bytes=12,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    matching_document = KnowledgeDocument(
        id=102,
        subject="物理",
        filename="matching.pdf",
        file_path="/tmp/matching.pdf",
        mime_type="application/pdf",
        size_bytes=12,
        resource_type=ResourceType.TEXTBOOK.value,
    )
    wrong_row = KnowledgeChunk(
        document_id=wrong_document.id,
        subject="物理",
        chunk_index=0,
        content="质点是用来描述物体运动的理想模型。",
        metadata_json={
            "document_id": wrong_document.id,
            "resource_type": ResourceType.TEXTBOOK.value,
            "chapter": "第二章 匀变速直线运动",
            "section": "1.速度变化快慢的描述",
            "structure_path": ["第二章 匀变速直线运动", "1.速度变化快慢的描述"],
        },
    )
    matching_row = KnowledgeChunk(
        document_id=matching_document.id,
        subject="物理",
        chunk_index=0,
        content="质点是用来描述物体运动的理想模型。",
        metadata_json={
            "document_id": matching_document.id,
            "resource_type": ResourceType.TEXTBOOK.value,
            "chapter": "第一章运动的描述",
            "section": "1.质点参考系",
            "structure_path": ["第一章运动的描述", "1.质点参考系"],
        },
    )
    wrong_row.document = wrong_document
    matching_row.document = matching_document

    ordered = rag_service._rerank_rows(
        question=question,
        profile=profile,
        scored_rows=[(1.0, wrong_row), (1.0, matching_row)],
        student_grade=None,
    )

    assert [row.document_id for row in ordered[:2]] == [matching_document.id, wrong_document.id]


def test_extract_text_supports_markdown_files(tmp_path):
    rag_service = build_rag_service(tmp_path)
    source_file = tmp_path / "formula.md"
    source_file.write_text(
        "# 机械能守恒\n\n"
        "公式：$$E\\_k\\=\\frac{1}{2}mv^2$$\n"
        "一个质量为\n"
        "$15kg$\n"
        "的\n"
        "$4$\n"
        "岁儿童从\n"
        "$9m$\n"
        "高处坠落。\n",
        encoding="utf-8",
    )

    extracted = rag_service.extract_text(str(source_file), "text/markdown")

    assert "机械能守恒" in extracted
    assert "$$E_k=\\frac{1}{2}mv^2$$" in extracted
    assert "一个质量为 $15kg$ 的 $4$ 岁儿童从 $9m$ 高处坠落。" in extracted


def test_prepare_chunks_keeps_inline_markdown_math_on_the_same_line(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=1,
        subject="物理",
        filename="时间与位移.md",
        file_path=str(tmp_path / "time.md"),
        mime_type="text/markdown",
        size_bytes=0,
        resource_type=ResourceType.KNOWLEDGE_NOTE.value,
    )
    text = (
        "一个质量为 $15kg$ 的 $4$ 岁儿童从 $9m$ 高处坠落，"
        "触手时刻的速度高达 $13.4m/s$。\n\n"
        "公式：$$E_k=\\frac{1}{2}mv^2$$"
    )

    chunks = rag_service.prepare_document_chunks(document, text)
    combined = "\n".join(chunk.content for chunk in chunks)

    assert "一个质量为 $15kg$ 的 $4$ 岁儿童从 $9m$ 高处坠落，触手时刻的速度高达 $13.4m/s$。" in combined


def test_extract_text_supports_native_docx_equations(tmp_path):
    rag_service = build_rag_service(tmp_path)
    source_file = tmp_path / "equation.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p>
      <w:r><w:t>动能近似：</w:t></w:r>
      <m:oMath>
        <m:sSup>
          <m:e><m:r><m:t>x</m:t></m:r></m:e>
          <m:sup><m:r><m:t>2</m:t></m:r></m:sup>
        </m:sSup>
        <m:r><m:t>+</m:t></m:r>
        <m:f>
          <m:num><m:r><m:t>1</m:t></m:r></m:num>
          <m:den><m:r><m:t>2</m:t></m:r></m:den>
        </m:f>
      </m:oMath>
    </w:p>
    <w:p>
      <m:oMathPara>
        <m:oMath>
          <m:rad>
            <m:e>
              <m:r><m:t>a+b</m:t></m:r>
            </m:e>
          </m:rad>
        </m:oMath>
      </m:oMathPara>
    </w:p>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(source_file, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    extracted = rag_service.extract_text(
        str(source_file),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert "动能近似：" in extracted
    assert "${x}^{2}+\\frac{1}{2}$" in extracted
    assert "$$\\sqrt{a+b}$$" in extracted


def test_recommend_questions_prefers_question_chunks_with_matching_grade_and_images(tmp_path):
    rag_service = build_rag_service(tmp_path)
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        grade2_doc = KnowledgeDocument(
            subject="物理",
            filename="grade2.docx",
            file_path="/tmp/grade2.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=12,
            resource_type=ResourceType.QUESTION_SET.value,
            grade=2,
        )
        grade3_doc = KnowledgeDocument(
            subject="物理",
            filename="grade3.docx",
            file_path="/tmp/grade3.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=12,
            resource_type=ResourceType.QUESTION_SET.value,
            grade=3,
        )
        note_doc = KnowledgeDocument(
            subject="物理",
            filename="note.txt",
            file_path="/tmp/note.txt",
            mime_type="text/plain",
            size_bytes=12,
            resource_type=ResourceType.KNOWLEDGE_NOTE.value,
            grade=2,
        )
        session.add_all([grade2_doc, grade3_doc, note_doc])
        session.commit()
        session.refresh(grade2_doc)
        session.refresh(grade3_doc)
        session.refresh(note_doc)

        session.add_all(
            [
                KnowledgeChunk(
                    document_id=grade2_doc.id,
                    subject="物理",
                    chunk_index=0,
                    content="第1题\n\n题目：如图所示斜面受力分析，求加速度。",
                    metadata_json={
                        "document_id": grade2_doc.id,
                        "resource_type": grade2_doc.resource_type,
                        "grade": 2,
                        "chunk_kind": "question_item",
                        "question_number": "1",
                        "question_text": "如图所示斜面受力分析，求加速度。",
                        "contains_images": True,
                        "image_count": 1,
                        "asset_refs": [{"asset_id": "image-001", "filename": "image-001.png", "content_type": "image/png", "url": "/api/knowledge/documents/1/assets/image-001.png"}],
                    },
                ),
                KnowledgeChunk(
                    document_id=grade3_doc.id,
                    subject="物理",
                    chunk_index=0,
                    content="第2题\n\n题目：电磁感应综合题，求感应电流。",
                    metadata_json={
                        "document_id": grade3_doc.id,
                        "resource_type": grade3_doc.resource_type,
                        "grade": 3,
                        "chunk_kind": "question_item",
                        "question_number": "2",
                        "question_text": "电磁感应综合题，求感应电流。",
                        "contains_images": False,
                        "image_count": 0,
                    },
                ),
                KnowledgeChunk(
                    document_id=note_doc.id,
                    subject="物理",
                    chunk_index=0,
                    content="受力分析要先画受力图。",
                    metadata_json={
                        "document_id": note_doc.id,
                        "resource_type": note_doc.resource_type,
                        "grade": 2,
                    },
                ),
            ]
        )
        session.commit()

        result = rag_service.recommend_questions(session, "物理", "如图所示这道受力分析题怎么练", student_grade=2, limit=2)

        assert len(result) == 2
        assert result[0].document_id == grade2_doc.id
        assert result[0].metadata_json.get("contains_images") is True
        assert all(row.document.resource_type in {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value} for row in result)
    finally:
        session.close()


def test_recommend_questions_suppresses_same_document_legacy_fallback_when_question_items_exist(tmp_path):
    rag_service = build_rag_service(tmp_path)
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        preferred_doc = KnowledgeDocument(
            subject="物理",
            filename="preferred.docx",
            file_path="/tmp/preferred.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=12,
            resource_type=ResourceType.QUESTION_SET.value,
            grade=2,
        )
        fallback_doc = KnowledgeDocument(
            subject="物理",
            filename="legacy.docx",
            file_path="/tmp/legacy.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=12,
            resource_type=ResourceType.QUESTION_SET.value,
            grade=2,
        )
        session.add_all([preferred_doc, fallback_doc])
        session.commit()
        session.refresh(preferred_doc)
        session.refresh(fallback_doc)

        preferred_row = KnowledgeChunk(
            document_id=preferred_doc.id,
            subject="物理",
            chunk_index=0,
            content="第1题\n\n题目：如图所示斜面受力分析，求加速度。",
            metadata_json={
                "document_id": preferred_doc.id,
                "resource_type": preferred_doc.resource_type,
                "grade": 2,
                "chunk_kind": "question_item",
                "question_number": "1",
                "question_text": "如图所示斜面受力分析，求加速度。",
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
        )
        same_doc_legacy_row = KnowledgeChunk(
            document_id=preferred_doc.id,
            subject="物理",
            chunk_index=1,
            content="如图所示斜面受力分析练习（旧分块）",
            metadata_json={
                "document_id": preferred_doc.id,
                "resource_type": preferred_doc.resource_type,
                "grade": 2,
                "chunk_kind": "",
                "question_text": "如图所示斜面受力分析练习（旧分块）",
            },
        )
        fallback_row = KnowledgeChunk(
            document_id=fallback_doc.id,
            subject="物理",
            chunk_index=0,
            content="如图所示受力分析题（仅旧分块）",
            metadata_json={
                "document_id": fallback_doc.id,
                "resource_type": fallback_doc.resource_type,
                "grade": 2,
                "chunk_kind": "",
                "question_text": "如图所示受力分析题（仅旧分块）",
            },
        )
        session.add_all([preferred_row, same_doc_legacy_row, fallback_row])
        session.commit()
        session.refresh(preferred_row)
        session.refresh(same_doc_legacy_row)
        session.refresh(fallback_row)

        result = rag_service.recommend_questions(session, "物理", "如图所示这类斜面受力分析题再来一题", student_grade=2, limit=2)

        assert [row.id for row in result] == [preferred_row.id, fallback_row.id]
        assert all(row.id != same_doc_legacy_row.id for row in result)
    finally:
        session.close()


def test_prepare_question_chunks_keeps_docx_images_and_pairs_answers(tmp_path):
    rag_service = build_rag_service(tmp_path)
    source_file = tmp_path / "question_bank.docx"
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0L8AAAAASUVORK5CYII="
    )
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
  <w:body>
    <w:p><w:r><w:t>1. 如图所示，小球沿斜面下滑，求加速度。</w:t></w:r></w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:docPr id="1" name="斜面示意图" descr="斜面小球模型"/>
            <a:graphic>
              <a:graphicData>
                <pic:pic>
                  <pic:blipFill>
                    <a:blip r:embed="rId1"/>
                  </pic:blipFill>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p><w:r><w:t>2. 已知 v-t 图像，求位移。</w:t></w:r></w:p>
    <w:p><w:r><w:t>参考答案</w:t></w:r></w:p>
    <w:p><w:r><w:t>1. 答案：a=g\\sin\\theta</w:t></w:r></w:p>
    <w:p><w:r><w:t>解析：由受力分析可得。</w:t></w:r></w:p>
    <w:p><w:r><w:t>2. 答案：位移等于图像与坐标轴围成的面积。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>
"""
    with zipfile.ZipFile(source_file, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", rels_xml)
        archive.writestr("word/media/image1.png", tiny_png)

    document = KnowledgeDocument(
        id=7,
        subject="物理",
        filename="question_bank.docx",
        file_path=str(source_file),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=source_file.stat().st_size,
        resource_type=ResourceType.QUESTION_SET.value,
    )
    extracted = rag_service.extract_content(
        str(source_file),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        document_id=document.id,
    )

    prepared = rag_service.prepare_document_chunks(document, extracted.text, assets=extracted.assets)

    assert len(prepared) == 2
    assert extracted.assets
    assert (tmp_path / "tasks" / "knowledge" / "7" / "image-001.png").is_file()
    assert prepared[0].metadata["question_number"] == "1"
    assert prepared[0].metadata["contains_images"] is True
    assert prepared[0].metadata["asset_refs"][0]["url"] == "/api/knowledge/documents/7/assets/image-001.png"
    assert prepared[0].metadata["source_format"] == "docx"
    assert prepared[0].metadata["source_locator"]
    assert prepared[0].metadata["image_expectation"] == "required"
    assert prepared[0].metadata["image_binding_status"] == "bound"
    assert "missing_required_image" not in prepared[0].metadata.get("quality_flags", [])
    assert prepared[0].metadata["question_uid"]
    assert "quality_score" not in prepared[0].metadata
    assert "【附图1：斜面示意图】" in prepared[0].content
    assert "答案：" in prepared[0].content
    assert "解析：" in prepared[0].content
    assert prepared[1].metadata["question_number"] == "2"
    assert prepared[1].metadata["source_format"] == "docx"
    assert prepared[1].metadata["question_uid"] == "qb:7:question:2"
    assert prepared[1].metadata["image_expectation"] == "optional"
    assert prepared[1].metadata["image_binding_status"] == "optional_unbound"
    assert "位移等于图像与坐标轴围成的面积" in prepared[1].content


def test_prepare_question_chunks_marks_missing_required_images_without_quality_score(tmp_path):
    rag_service = build_rag_service(tmp_path)
    document = KnowledgeDocument(
        id=11,
        subject="物理",
        filename="missing-image.docx",
        file_path=str(tmp_path / "missing-image.docx"),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=12,
        resource_type=ResourceType.QUESTION_SET.value,
    )

    prepared = rag_service.prepare_document_chunks(document, "1. 如图所示，分析小球在斜面上的受力。")

    assert len(prepared) == 1
    metadata = prepared[0].metadata
    assert metadata["source_format"] == "docx"
    assert metadata["source_locator"]
    assert metadata["image_expectation"] == "required"
    assert metadata["image_binding_status"] == "missing_required"
    assert "missing_required_image" in metadata["quality_flags"]
    assert metadata["question_uid"]
    assert "quality_score" not in metadata


def test_vector_store_metadata_whitelists_structure_retrieval_fields(tmp_path):
    rag_service = build_rag_service(tmp_path)
    row = KnowledgeChunk(
        id=9,
        document_id=7,
        subject="物理",
        chunk_index=0,
        content="参考系用于描述物体的位置。",
        metadata_json={
            "resource_type": ResourceType.TEXTBOOK.value,
            "chapter": "第一章运动的描述",
            "section": "1.质点参考系",
            "structure_source": "toc_page_map",
            "parser_provenance": {"runtime_artifact": "data/tasks/42/mineru-runtime.json"},
            "retrieval_metadata": {
                "chapter_key": "第一章运动的描述",
                "section_key": "1质点参考系",
                "page_start": 3,
                "page_end": 3,
                "structure_path": ["第一章运动的描述", "1.质点参考系"],
            },
            "diagnostic_metadata": {"parser_backend": "pipeline"},
        },
    )

    metadata = rag_service.vector_store._build_metadata(row)

    assert metadata["chapter"] == "第一章运动的描述"
    assert metadata["section"] == "1.质点参考系"
    assert metadata["chapter_key"] == "第一章运动的描述"
    assert metadata["section_key"] == "1质点参考系"
    assert metadata["page_start"] == 3
    assert metadata["structure_path_text"] == "第一章运动的描述 > 1.质点参考系"
    assert "parser_provenance" not in metadata
    assert "diagnostic_metadata" not in metadata


def test_retrieve_prefers_chunk_with_matching_chunk_level_structure(tmp_path):
    rag_service = build_rag_service(tmp_path)
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        doc = KnowledgeDocument(
            subject="物理",
            filename="textbook.txt",
            file_path="/tmp/textbook.txt",
            mime_type="text/plain",
            size_bytes=12,
            resource_type=ResourceType.TEXTBOOK.value,
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)

        matching = KnowledgeChunk(
            document_id=doc.id,
            subject="物理",
            chunk_index=0,
            content="参考系用于描述物体的位置和运动。",
            metadata_json={
                "resource_type": ResourceType.TEXTBOOK.value,
                "chapter": "第一章运动的描述",
                "section": "1.质点参考系",
                "retrieval_metadata": {
                    "chapter": "第一章运动的描述",
                    "section": "1.质点参考系",
                    "chapter_key": "第一章运动的描述",
                    "section_key": "1质点参考系",
                    "structure_path": ["第一章运动的描述", "1.质点参考系"],
                    "structure_source": "toc_page_map",
                },
            },
        )
        other = KnowledgeChunk(
            document_id=doc.id,
            subject="物理",
            chunk_index=1,
            content="参考系用于描述物体的位置和运动。",
            metadata_json={
                "resource_type": ResourceType.TEXTBOOK.value,
                "chapter": "第二章匀变速直线运动的研究",
                "section": "1.速度变化快慢的描述",
                "retrieval_metadata": {
                    "chapter": "第二章匀变速直线运动的研究",
                    "section": "1.速度变化快慢的描述",
                    "chapter_key": "第二章匀变速直线运动的研究",
                    "section_key": "1速度变化快慢的描述",
                    "structure_path": ["第二章匀变速直线运动的研究", "1.速度变化快慢的描述"],
                    "structure_source": "toc_page_map",
                },
            },
        )
        session.add_all([matching, other])
        session.commit()
        session.refresh(matching)
        session.refresh(other)
        rag_service.vector_store.upsert_chunks("物理", [matching, other])

        result = rag_service.retrieve(session, "物理", "第一章运动的描述里1.质点参考系讲什么")

        assert result.chunks
        assert result.chunks[0].id == matching.id
    finally:
        session.close()
