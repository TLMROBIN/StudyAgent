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


def test_extract_text_supports_markdown_files(tmp_path):
    rag_service = build_rag_service(tmp_path)
    source_file = tmp_path / "formula.md"
    source_file.write_text("# 机械能守恒\n\n公式：$$E_k=\\frac{1}{2}mv^2$$", encoding="utf-8")

    extracted = rag_service.extract_text(str(source_file), "text/markdown")

    assert "机械能守恒" in extracted
    assert "$$E_k=\\frac{1}{2}mv^2$$" in extracted


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
    assert "【附图1：斜面示意图】" in prepared[0].content
    assert "答案：" in prepared[0].content
    assert "解析：" in prepared[0].content
    assert prepared[1].metadata["question_number"] == "2"
    assert "位移等于图像与坐标轴围成的面积" in prepared[1].content
