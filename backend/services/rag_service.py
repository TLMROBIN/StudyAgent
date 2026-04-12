from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path, PurePosixPath
import mimetypes
import posixpath
import re
import shutil
import struct
from typing import Any, Callable
from xml.etree import ElementTree as ET
import zipfile

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from backend.config import Settings, get_settings
from backend.grade_utils import HIGH_SCHOOL_GRADE_LABELS, extract_grade_levels
from backend.models.knowledge import DifficultyLevel, KnowledgeChunk, KnowledgeDocument, ResourceType
from backend.services.embed_service import EmbedService, embed_service
from backend.services.mineru_service import mineru_service
from backend.services.pdf_parse_bridge import PDFParseBridge
from backend.services.pdf_parse_types import ExtractedAsset, PDFParseResult
from backend.services.question_bank_post_processor import QuestionBankChunkCandidate, QuestionBankPostProcessor
from backend.services.vector_store_service import VectorStoreService, vector_store_service

logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}
CHAPTER_AWARE_RESOURCE_TYPES = {
    ResourceType.KNOWLEDGE_NOTE.value,
    ResourceType.TEXTBOOK.value,
    ResourceType.EXERCISE.value,
    ResourceType.QUESTION_SET.value,
}
LEGACY_QUESTION_DOCX_FORMULA_MESSAGE = (
    "检测到 MathType 类 legacy 公式，当前不支持；请改用微软公式（OMML）后重新导入"
)
LEGACY_DOCX_OLE_PROG_IDS = {
    "Equation.DSMT4",
    "MathType 6.0 Equation",
    "MathType 7.0 Equation",
    "MathType EF",
}
CHAPTER_HEADING_PATTERNS = [
    re.compile(r"^(第[一二三四五六七八九十百零两0-9]+(?:章|单元|编|部分))(?:\s*[-—－:：]?\s*\S.*)?$"),
    re.compile(r"^(专题[一二三四五六七八九十百零两0-9]+.*)$"),
    re.compile(r"^(Unit\s+\d+.*)$", re.IGNORECASE),
]
DECIMAL_SECTION_HEADING_PATTERN = re.compile(r"^[0-9]{1,2}(?:\.[0-9]{1,2})+\s*\S.*$")
GENERIC_SINGLE_LEVEL_SECTION_PATTERN = re.compile(r"^[0-9]{1,2}[.．、]\s*\S.*$")
SECTION_HEADING_PATTERNS = [
    re.compile(r"^(第[一二三四五六七八九十百零两0-9]+(?:节|课))(?:\s*[-—－:：]?\s*\S.*)?$"),
    DECIMAL_SECTION_HEADING_PATTERN,
    GENERIC_SINGLE_LEVEL_SECTION_PATTERN,
    re.compile(r"^[（(][一二三四五六七八九十百零两0-9]+[)）]\s*\S.*$"),
    re.compile(r"^[A-Za-z][.、]\s*\S.*$"),
]
QUESTION_LIKE_HEADING_HINTS = (
    "？",
    "?",
    "求",
    "多少",
    "下列",
    "正确",
    "错误",
    "判断",
    "计算",
    "解答",
    "写出",
    "指出",
    "求出",
)
TEXTBOOK_BACK_MATTER_HEADINGS = {
    "课题研究",
    "学生实验",
    "索引",
    "后记",
    "附录",
}
TEXTBOOK_SENTENCE_PUNCTUATION = ("，", "；", "。", "！", "？", ",", ";", "!", "?")
PLAIN_TEXT_SPLIT_PATTERN = re.compile(r"(?<=[。！？；;.!?])\s+|\n+")
CHUNK_BOUNDARY_HINTS = "。！？；;.!?，,"
ASSET_MARKER_PATTERN = re.compile(r"\[\[asset:([A-Za-z0-9_.-]+)\]\]")
MARKDOWN_BLOCK_START_PATTERN = re.compile(
    r"^(?:#{1,6}\s|[*+-]\s|[0-9]{1,2}(?:\\?\.)\s|[0-9]{1,2}[)）]\s|>\s|```|~~~|\|)"
)
INLINE_MATH_TERMINAL_PATTERN = re.compile(r"(?:\$(?!\$)[^$]+\$|\\\([^)]*\\\))$")
PUNCTUATION_ONLY_PATTERN = re.compile(r"^[，。！？；：、,.;!?）)】》]+$")
DOCX_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
DOCX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCX_OLE_NS = "urn:schemas-microsoft-com:office:office"
DOCX_VML_NS = "urn:schemas-microsoft-com:vml"
OLE_END_OF_CHAIN = 0xFFFFFFFE
OLE_FREE_SECTOR = 0xFFFFFFFF
OLE_UINT32_SIZE = 4
OLE_CF_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OLE_METADATA_STRINGS = {
    "MathType 6.0 Equation",
    "MathType 7.0 Equation",
    "MathType EF",
    "Equation.DSMT4",
    "DSMT6",
    "DSMT7",
    "WinAllBasicCodePages",
    "WinAllCodePages",
    "Times New Roman",
    "Symbol",
    "Courier New",
    "MT Extra",
    "Root Entry",
    "Ole",
    "CompObj",
    "ObjInfo",
    "Equation Native",
    "OlePres000",
    "AppsMFCC",
    "Design Science, Inc.",
    "System",
}
OMML_OPERATOR_MAP = {
    "∑": r"\sum",
    "∏": r"\prod",
    "∐": r"\coprod",
    "∫": r"\int",
    "∮": r"\oint",
    "⋂": r"\bigcap",
    "⋃": r"\bigcup",
}
OMML_DELIMITER_MAP = {
    "{": r"\{",
    "}": r"\}",
    "⟨": r"\langle",
    "⟩": r"\rangle",
    "⌊": r"\lfloor",
    "⌋": r"\rfloor",
    "⌈": r"\lceil",
    "⌉": r"\rceil",
}
OMML_ACCENT_MAP = {
    "̂": r"\hat",
    "^": r"\hat",
    "̄": r"\bar",
    "¯": r"\bar",
    "→": r"\vec",
    "⃗": r"\vec",
    "˙": r"\dot",
    "¨": r"\ddot",
    "̃": r"\tilde",
}
QUESTION_START_PATTERN = re.compile(
    r"^\s*(?:第\s*(?P<ordinal>\d+)\s*题|(?P<plain>\d{1,3})\s*[.．、:：)]|[（(](?P<wrapped>\d{1,3})[)）])\s*(?P<body>.*)$"
)
QUESTION_SECTION_HEADING_PATTERN = re.compile(
    r"^\s*(?:【)?(?:参考)?(?:答案(?:与解析)?|答案及解析|解答|参考解答|参考解析|解析|详解)(?:】)?\s*$"
)
ANSWER_LINE_PATTERN = re.compile(r"^\s*(?:【)?(?:参考)?答案(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
EXPLANATION_LINE_PATTERN = re.compile(r"^\s*(?:【)?(?:解析|详解|解答|思路(?:点拨)?|点拨|说明|分析|点睛)(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
DIFFICULTY_LINE_PATTERN = re.compile(r"^\s*(?:【)?难度(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
KNOWLEDGE_POINTS_LINE_PATTERN = re.compile(r"^\s*(?:【)?知识点(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
QUESTION_CATEGORY_HEADING_PATTERN = re.compile(
    r"^\s*[一二三四五六七八九十]+[、.．]\s*(?:单选题|多选题|填空题|选择题|判断题|解答题|计算题|实验题|综合题|简答题)\s*$"
)
QUESTION_METADATA_PRESERVE_KEYS = {
    "chunk_kind",
    "question_number",
    "question_text",
    "answer_text",
    "explanation_text",
    "difficulty",
    "tags",
    "chapter",
    "section",
    "contains_images",
    "asset_refs",
    "image_count",
    "parser_backend",
    "parser_provenance",
    "source_format",
    "source_locator",
    "page_start",
    "page_end",
    "source_pages",
    "source_block_types",
    "structure_path",
    "image_expectation",
    "image_binding_status",
    "quality_flags",
    "question_uid",
    "chapter_key",
    "section_key",
    "structure_source",
    "structure_confidence",
    "retrieval_metadata",
    "diagnostic_metadata",
    "ingestion_metadata",
}


@dataclass
class RetrievalResult:
    context: str
    chunks: list[KnowledgeChunk]


@dataclass
class PDFExtractionCandidate:
    extractor: str
    pages: list[str]


@dataclass
class ExtractionResult:
    text: str
    assets: list[ExtractedAsset] = field(default_factory=list)
    parsed_pdf: PDFParseResult | None = None
    parser_backend: str | None = None
    parser_provenance: dict[str, Any] | None = None
    source_format: str | None = None


class UnsupportedQuestionDocxError(RuntimeError):
    """Raised when a question-resource DOCX contains unsupported legacy formulas."""


@dataclass
class PreparedChunk:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuestionChunkDraft:
    question_number: str
    question_text: str
    answer_text: str | None = None
    explanation_text: str | None = None
    asset_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QuestionProfile:
    question_type: str
    preferred_resources: list[str]
    desired_difficulty: str | None = None
    prefer_extension: bool = False


class RagService:
    def __init__(
        self,
        settings: Settings | None = None,
        embedder: EmbedService | None = None,
        vector_store: VectorStoreService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or embed_service
        self.vector_store = vector_store or vector_store_service
        self.QUESTION_RESOURCE_TYPES = QUESTION_RESOURCE_TYPES
        self.PreparedChunk = PreparedChunk
        self.question_bank_post_processor = QuestionBankPostProcessor()
        self.pdf_parse_bridge = PDFParseBridge(self)

    def split_text(self, text: str) -> list[str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []
        return self._split_formula_aware_text(text)

    def retrieve(self, db: Session, subject: str, question: str, *, student_grade: int | None = None) -> RetrievalResult:
        profile = self._infer_question_profile(question)
        try:
            matches = self.vector_store.query(subject, question, max(self.settings.rag_top_k * 4, self.settings.rag_top_k))
            if matches:
                chunk_ids = [match.chunk_id for match in matches]
                rows = db.scalars(
                    select(KnowledgeChunk)
                    .options(selectinload(KnowledgeChunk.document))
                    .where(KnowledgeChunk.id.in_(chunk_ids))
                ).all()
                row_map = {row.id: row for row in rows}
                scored_rows = []
                for index, match in enumerate(matches):
                    row = row_map.get(match.chunk_id)
                    if not row:
                        continue
                    distance = match.distance if match.distance is not None else 0.8
                    base_score = max(0.0, 1.1 - float(distance)) + max(0.0, 0.12 - index * 0.01)
                    scored_rows.append((base_score, row))
                ordered_rows = self._rerank_rows(
                    question=question,
                    profile=profile,
                    scored_rows=scored_rows,
                    student_grade=student_grade,
                )
                if ordered_rows:
                    return RetrievalResult(context=self._format_context(ordered_rows), chunks=ordered_rows)
        except Exception:
            pass

        return self._fallback_retrieve(db, subject, question, profile=profile, student_grade=student_grade)

    def recommend_questions(
        self,
        db: Session,
        subject: str,
        question: str,
        *,
        student_grade: int | None = None,
        limit: int = 3,
    ) -> list[KnowledgeChunk]:
        rows = db.scalars(
            select(KnowledgeChunk)
            .options(selectinload(KnowledgeChunk.document))
            .where(KnowledgeChunk.subject == subject)
        ).all()
        preferred_rows = [row for row in rows if self._question_row_tier(row) == "preferred"]
        if not preferred_rows and not any(self._question_row_tier(row) == "fallback" for row in rows):
            return []

        query_embedding = self.embedder.embed_text(question)
        profile = self._recommendation_profile(question)
        selected_rows: list[KnowledgeChunk] = []
        seen_keys: set[tuple[int, str]] = set()
        for _, row in self._score_question_rows(preferred_rows, question, student_grade, query_embedding, profile):
            key = self._question_row_key(row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected_rows.append(row)
            if len(selected_rows) >= limit:
                return selected_rows

        preferred_document_ids = {row.document_id for row in preferred_rows}
        fallback_rows = [
            row
            for row in rows
            if self._question_row_tier(row) == "fallback" and row.document_id not in preferred_document_ids
        ]
        for _, row in self._score_question_rows(fallback_rows, question, student_grade, query_embedding, profile):
            key = self._question_row_key(row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected_rows.append(row)
            if len(selected_rows) >= limit:
                break
        return selected_rows

    def _fallback_retrieve(
        self,
        db: Session,
        subject: str,
        question: str,
        *,
        profile: QuestionProfile | None = None,
        student_grade: int | None = None,
    ) -> RetrievalResult:
        rows = db.scalars(
            select(KnowledgeChunk)
            .options(selectinload(KnowledgeChunk.document))
            .where(KnowledgeChunk.subject == subject)
        ).all()
        if not rows:
            return RetrievalResult(context="", chunks=[])

        query_embedding = self.embedder.embed_text(question)
        scored_rows = []
        for row in rows:
            score = self.embedder.cosine_similarity(query_embedding, self.embedder.embed_text(row.content))
            keyword_bonus = sum(1 for char in question[:12] if char and char in row.content) / 20.0
            scored_rows.append((score + keyword_bonus, row))
        best = self._rerank_rows(
            question=question,
            profile=profile or self._infer_question_profile(question),
            scored_rows=scored_rows,
            student_grade=student_grade,
        )
        return RetrievalResult(context=self._format_context(best), chunks=best)

    def ingest_document_text(self, db: Session, document: KnowledgeDocument, text: str) -> int:
        prepared_chunks = self.prepare_document_chunks(document, text)
        if not prepared_chunks:
            raise RuntimeError("文档未提取到可用文本，未生成任何索引内容")
        progress_callback: Callable[[int, str], None] | None = None
        return self.ingest_document_chunks(db, document, prepared_chunks, progress_callback=progress_callback)

    def ingest_document_chunks(
        self,
        db: Session,
        document: KnowledgeDocument,
        chunks: list[str] | list[PreparedChunk],
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> int:
        prepared_chunks = self._coerce_prepared_chunks(document, chunks)
        if progress_callback:
            progress_callback(40, f"文本切分完成，共 {len(prepared_chunks)} 个片段")
        self.vector_store.delete_document(document.subject, document.id)
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        db.commit()

        created_rows: list[KnowledgeChunk] = []
        try:
            for index, chunk in enumerate(prepared_chunks):
                row = KnowledgeChunk(
                    document_id=document.id,
                    subject=document.subject,
                    chunk_index=index,
                    content=chunk.content,
                    metadata_json=chunk.metadata,
                )
                db.add(row)
                created_rows.append(row)
            db.commit()
            if progress_callback:
                progress_callback(65, f"已写入数据库，共 {len(created_rows)} 个片段")

            created_rows = db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id)
                .order_by(KnowledgeChunk.chunk_index.asc())
            ).all()
            if progress_callback:
                progress_callback(85, "正在写入向量库")
            self.vector_store.upsert_chunks(document.subject, created_rows)
            if progress_callback:
                progress_callback(95, "向量索引写入完成")
            return len(created_rows)
        except Exception:
            db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
            db.commit()
            raise

    def purge_document_index(self, db: Session, document: KnowledgeDocument) -> None:
        self.vector_store.delete_document(document.subject, document.id)
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        db.commit()

    def sync_document_metadata(self, db: Session, document: KnowledgeDocument) -> None:
        rows = db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document.id)
            .order_by(KnowledgeChunk.chunk_index.asc())
        ).all()
        if not rows:
            return

        current_chapter = document.chapter
        current_section = document.section
        for row in rows:
            heading_context = self._extract_heading_context(row.content, document.resource_type)
            if heading_context["chapter"]:
                current_chapter = heading_context["chapter"]
                current_section = None
            if heading_context["section"]:
                current_section = heading_context["section"]
            existing_metadata = row.metadata_json or {}
            preserved = {
                key: value
                for key, value in existing_metadata.items()
                if key in QUESTION_METADATA_PRESERVE_KEYS
            }
            row.metadata_json = self._build_chunk_metadata(
                document,
                chapter=current_chapter,
                section=current_section,
                extra_metadata=preserved,
            )
            db.add(row)
        db.commit()
        self.vector_store.upsert_chunks(document.subject, rows)

    def prepare_document_chunks(
        self,
        document: KnowledgeDocument,
        text: str,
        *,
        assets: list[ExtractedAsset] | None = None,
        parsed_pdf: PDFParseResult | None = None,
        parser_backend: str | None = None,
        parser_provenance: dict[str, Any] | None = None,
        source_format: str | None = None,
    ) -> list[PreparedChunk]:
        if parsed_pdf is not None:
            return self.pdf_parse_bridge.prepare_chunks(document, parsed_pdf)
        asset_map = {asset.asset_id: asset for asset in assets or []}
        if (document.resource_type or ResourceType.KNOWLEDGE_NOTE.value) in QUESTION_RESOURCE_TYPES:
            prepared_questions = self._prepare_question_chunks(
                document,
                text,
                asset_map,
                source_format=source_format,
                parser_backend=parser_backend,
                parser_provenance=parser_provenance,
            )
            if prepared_questions:
                return prepared_questions
        segments = self._segment_text_with_context(text, document)
        prepared_chunks: list[PreparedChunk] = []
        for segment in segments:
            for chunk in self.split_text(segment["text"]):
                content, asset_refs = self._finalize_chunk_text_and_assets(chunk, asset_map)
                if not content.strip():
                    continue
                prepared_chunks.append(
                    PreparedChunk(
                        content=content,
                        metadata=self._build_chunk_metadata(
                            document,
                            chapter=segment.get("chapter"),
                            section=segment.get("section"),
                            extra_metadata={
                                "contains_images": bool(asset_refs),
                                "asset_refs": asset_refs,
                                "image_count": len(asset_refs),
                            },
                        ),
                    )
                )
        return prepared_chunks

    def extract_text(self, file_path: str, mime_type: str | None = None) -> str:
        return self.extract_content(file_path, mime_type=mime_type).text

    def extract_content(
        self,
        file_path: str,
        mime_type: str | None = None,
        *,
        document_id: int | None = None,
        task_id: int | None = None,
        resource_type: ResourceType | str | None = None,
    ) -> ExtractionResult:
        mime = (mime_type or mimetypes.guess_type(file_path)[0] or "").lower()
        suffix = Path(file_path).suffix.lower()
        if suffix in {".txt", ".md", ".tex"} or mime in {"text/plain", "text/markdown", "text/x-markdown", "text/x-tex", "application/x-tex"}:
            text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
            if suffix == ".md" or mime in {"text/markdown", "text/x-markdown"}:
                text = self._normalize_markdown_text(text)
            return ExtractionResult(text=text)

        if suffix == ".pdf":
            if self.settings.pdf_parser_backend == "mineru":
                if document_id is None or task_id is None:
                    raise RuntimeError("MinerU PDF parsing requires document_id and task_id")
                parsed_pdf = mineru_service.parse_pdf(file_path, task_id=task_id, document_id=document_id)
                return ExtractionResult(
                    text=parsed_pdf.text,
                    assets=parsed_pdf.assets,
                    parsed_pdf=parsed_pdf,
                    parser_backend=parsed_pdf.parser_backend,
                    parser_provenance=parsed_pdf.parser_provenance,
                    source_format="pdf",
                )
            return ExtractionResult(text=self._extract_pdf_text(file_path))

        if suffix == ".docx" or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            if self._resource_type_value(resource_type) in QUESTION_RESOURCE_TYPES:
                self.ensure_question_resource_docx_supported(file_path)
                if document_id is None or task_id is None:
                    raise RuntimeError("MinerU DOCX parsing requires document_id and task_id")
                parsed_docx = mineru_service.parse_docx(file_path, task_id=task_id, document_id=document_id)
                return ExtractionResult(
                    text=parsed_docx.text,
                    assets=parsed_docx.assets,
                    parser_backend=parsed_docx.parser_backend,
                    parser_provenance=parsed_docx.parser_provenance,
                    source_format="docx",
                )
            return self._extract_docx_content(file_path, document_id=document_id)

        raise RuntimeError(f"暂不支持解析文件类型：{suffix}")

    def ensure_question_resource_docx_supported(self, file_path: str) -> None:
        if self._docx_contains_legacy_formula_objects(file_path):
            raise UnsupportedQuestionDocxError(LEGACY_QUESTION_DOCX_FORMULA_MESSAGE)

    def _resource_type_value(self, resource_type: ResourceType | str | None) -> str:
        if isinstance(resource_type, ResourceType):
            return resource_type.value
        return str(resource_type or "").strip()

    def _docx_contains_legacy_formula_objects(self, file_path: str) -> bool:
        try:
            with zipfile.ZipFile(file_path) as archive:
                for name in archive.namelist():
                    normalized = str(name or "")
                    if not normalized.startswith("word/") or not normalized.endswith(".xml"):
                        continue
                    try:
                        root = ET.fromstring(archive.read(normalized))
                    except (KeyError, ET.ParseError):
                        continue
                    for element in root.iter():
                        if (
                            self._xml_namespace(element.tag) == DOCX_OLE_NS
                            and self._xml_local_name(element.tag) == "OLEObject"
                            and str(element.attrib.get("ProgID") or "").strip() in LEGACY_DOCX_OLE_PROG_IDS
                        ):
                            return True
        except (KeyError, zipfile.BadZipFile):
            return False
        return False

    def health_snapshot(self) -> dict[str, dict | str | bool]:
        return {
            "embedding": self.embedder.health_snapshot(),
            "vector_store": self.vector_store.health_snapshot(),
            "pdf_parser": mineru_service.health_snapshot(),
        }

    def _normalize_markdown_text(self, text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\xa0", " ")
        normalized = self._normalize_markdown_math_spans(normalized)
        normalized = normalized.replace(r"\.", ".")
        normalized = self._collapse_soft_markdown_line_breaks(normalized)
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n[ \t]+", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        normalized = re.sub(r"[ \t]{2,}", " ", normalized)
        return normalized.strip()

    def _normalize_markdown_math_spans(self, text: str) -> str:
        parts: list[str] = []
        for token_type, content in self._extract_preserved_tokens(text):
            if token_type == "math":
                parts.append(self._normalize_markdown_math_token(content))
                continue
            parts.append(content)
        return "".join(parts)

    def _normalize_markdown_math_token(self, token: str) -> str:
        delimiters = [
            ("$$", "$$"),
            (r"\[", r"\]"),
            (r"\(", r"\)"),
            ("$", "$"),
        ]
        for opening, closing in delimiters:
            if token.startswith(opening) and token.endswith(closing):
                body = token[len(opening) : len(token) - len(closing)]
                body = re.sub(r"\\([_=.#])", r"\1", body)
                body = re.sub(r"\s+", " ", body).strip()
                return f"{opening}{body}{closing}"
        return token

    def _collapse_soft_markdown_line_breaks(self, text: str) -> str:
        normalized_lines: list[str] = []
        current = ""
        in_fence = False

        def flush_current() -> None:
            nonlocal current
            if current:
                normalized_lines.append(current.strip())
                current = ""

        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                flush_current()
                if normalized_lines and normalized_lines[-1] != "":
                    normalized_lines.append("")
                continue

            if line.startswith(("```", "~~~")):
                flush_current()
                normalized_lines.append(line)
                in_fence = not in_fence
                continue

            if in_fence:
                normalized_lines.append(line)
                continue

            if MARKDOWN_BLOCK_START_PATTERN.match(line):
                flush_current()
                current = line
                continue

            current = f"{current} {line}".strip() if current else line

        flush_current()
        return "\n".join(normalized_lines)

    def document_asset_dir(self, document_id: int) -> Path:
        return Path(self.settings.task_artifact_path) / "knowledge" / str(document_id)

    def clear_document_artifacts(self, document_id: int) -> None:
        shutil.rmtree(self.document_asset_dir(document_id), ignore_errors=True)

    def _split_formula_aware_text(self, text: str) -> list[str]:
        segments: list[str] = []
        current = ""
        for segment in self._chunkable_segments(text):
            normalized_segment = segment.strip()
            if not normalized_segment:
                continue
            if not current:
                current = normalized_segment
                continue

            candidate = self._join_chunkable_segments(current, normalized_segment)
            if len(candidate) <= self.settings.rag_chunk_size:
                current = candidate
                continue

            segments.append(current)
            current = normalized_segment

        if current:
            segments.append(current)
        return [segment for segment in segments if segment.strip()]

    def _join_chunkable_segments(self, current: str, next_segment: str) -> str:
        joiner = "\n"
        if self._should_inline_join_segment(current, next_segment):
            joiner = "" if PUNCTUATION_ONLY_PATTERN.fullmatch(next_segment.strip()) else " "
        combined = f"{current}{joiner}{next_segment}".strip()
        return re.sub(r"\s+([，。！？；：、,.;!?）)】》])", r"\1", combined)

    def _should_inline_join_segment(self, current: str, next_segment: str) -> bool:
        previous = current.strip()
        upcoming = next_segment.strip()
        if not previous or not upcoming:
            return False
        if self._is_display_math_segment(previous) or self._is_display_math_segment(upcoming):
            return False
        if MARKDOWN_BLOCK_START_PATTERN.match(upcoming):
            return False
        return (
            self._is_inline_math_segment(upcoming)
            or bool(INLINE_MATH_TERMINAL_PATTERN.search(previous))
        )

    def _is_inline_math_segment(self, segment: str) -> bool:
        stripped = segment.strip()
        return (
            (stripped.startswith("$") and not stripped.startswith("$$") and stripped.endswith("$"))
            or (stripped.startswith(r"\(") and stripped.endswith(r"\)"))
        )

    def _is_display_math_segment(self, segment: str) -> bool:
        stripped = segment.strip()
        return (
            (stripped.startswith("$$") and stripped.endswith("$$"))
            or (stripped.startswith(r"\[") and stripped.endswith(r"\]"))
        )

    def _chunkable_segments(self, text: str) -> list[str]:
        segments: list[str] = []
        for token_type, content in self._extract_preserved_tokens(text):
            if token_type == "text":
                segments.extend(self._split_plain_text_segments(content))
                continue
            stripped = content.strip()
            if stripped:
                segments.append(stripped)
        return segments

    def _extract_preserved_tokens(self, text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        cursor = 0
        plain_start = 0
        while cursor < len(text):
            matched = self._match_preserved_span(text, cursor)
            if not matched:
                cursor += 1
                continue

            token_type, end_index = matched
            if cursor > plain_start:
                tokens.append(("text", text[plain_start:cursor]))
            tokens.append((token_type, text[cursor:end_index]))
            cursor = end_index
            plain_start = end_index

        if plain_start < len(text):
            tokens.append(("text", text[plain_start:]))
        return tokens

    def _match_preserved_span(self, text: str, cursor: int) -> tuple[str, int] | None:
        if text.startswith("```", cursor):
            closing = text.find("```", cursor + 3)
            if closing != -1:
                return "code", closing + 3

        if text.startswith("$$", cursor) and not self._is_escaped(text, cursor):
            closing = self._find_unescaped_delimiter(text, "$$", cursor + 2)
            if closing != -1:
                return "math", closing + 2

        if text.startswith(r"\[", cursor) and not self._is_escaped(text, cursor):
            closing = self._find_unescaped_delimiter(text, r"\]", cursor + 2)
            if closing != -1:
                return "math", closing + 2

        if text.startswith(r"\(", cursor) and not self._is_escaped(text, cursor):
            closing = self._find_unescaped_delimiter(text, r"\)", cursor + 2)
            if closing != -1:
                return "math", closing + 2

        if text[cursor] == "$" and not text.startswith("$$", cursor) and not self._is_escaped(text, cursor):
            closing = self._find_unescaped_delimiter(text, "$", cursor + 1)
            if closing != -1:
                return "math", closing + 1

        return None

    def _split_plain_text_segments(self, text: str) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []

        paragraphs = [item.strip() for item in re.split(r"\n{2,}", stripped) if item.strip()]
        if not paragraphs:
            paragraphs = [stripped]

        segments: list[str] = []
        for paragraph in paragraphs:
            units = [item.strip() for item in PLAIN_TEXT_SPLIT_PATTERN.split(paragraph) if item.strip()]
            if not units:
                units = [paragraph]
            segments.extend(self._pack_plain_text_units(units))
        return segments

    def _pack_plain_text_units(self, units: list[str]) -> list[str]:
        packed: list[str] = []
        current = ""
        for unit in units:
            if len(unit) > self.settings.rag_chunk_size:
                if current:
                    packed.append(current)
                    current = ""
                packed.extend(self._split_long_plain_text(unit))
                continue

            candidate = f"{current}\n{unit}".strip() if current else unit
            if len(candidate) <= self.settings.rag_chunk_size:
                current = candidate
                continue

            if current:
                packed.append(current)
            current = unit

        if current:
            packed.append(current)
        return packed

    def _split_long_plain_text(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []

        max_size = self.settings.rag_chunk_size
        overlap = min(self.settings.rag_chunk_overlap, max(max_size // 3, 0))
        step = max(max_size - overlap, 1)
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_size, len(text))
            if end < len(text):
                boundary = max((text.rfind(marker, start + max_size // 2, end) for marker in CHUNK_BOUNDARY_HINTS), default=-1)
                if boundary > start:
                    end = boundary + 1
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def _is_escaped(self, text: str, index: int) -> bool:
        slash_count = 0
        cursor = index - 1
        while cursor >= 0 and text[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        return slash_count % 2 == 1

    def _find_unescaped_delimiter(self, text: str, delimiter: str, start_index: int) -> int:
        cursor = start_index
        while cursor < len(text):
            found = text.find(delimiter, cursor)
            if found == -1:
                return -1
            if not self._is_escaped(text, found):
                return found
            cursor = found + len(delimiter)
        return -1

    def _coerce_prepared_chunks(
        self,
        document: KnowledgeDocument,
        chunks: list[str] | list[PreparedChunk],
    ) -> list[PreparedChunk]:
        if not chunks:
            return []
        first_item = chunks[0]
        if isinstance(first_item, PreparedChunk):
            return [item for item in chunks if item.content.strip()]
        return [
            PreparedChunk(content=str(item), metadata=self._build_chunk_metadata(document))
            for item in chunks
            if str(item).strip()
        ]

    def _build_chunk_metadata(
        self,
        document: KnowledgeDocument,
        *,
        chapter: str | None = None,
        section: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tags = document.tags if hasattr(document, "tags") else []
        metadata = {
            "document_id": document.id,
            "filename": document.filename,
            "subject": document.subject,
            "resource_type": document.resource_type or ResourceType.KNOWLEDGE_NOTE.value,
            "grade": document.grade,
            "chapter": chapter or document.chapter,
            "section": section or document.section,
            "difficulty": document.difficulty,
            "tags": tags,
        }
        if extra_metadata:
            metadata.update({key: value for key, value in extra_metadata.items() if value is not None})
        return self._apply_metadata_layers(metadata)

    def _apply_metadata_layers(self, metadata: dict[str, Any]) -> dict[str, Any]:
        chapter = str(metadata.get("chapter") or "").strip() or None
        section = str(metadata.get("section") or "").strip() or None
        structure_path = metadata.get("structure_path")
        if not isinstance(structure_path, list):
            structure_path = [item for item in (chapter, section) if item]
        else:
            structure_path = [str(item).strip() for item in structure_path if str(item).strip()]
        chapter_key = self._structure_key(chapter)
        section_key = self._structure_key(section)

        retrieval_metadata = dict(metadata.get("retrieval_metadata") or {})
        retrieval_metadata.update(
            {
                key: value
                for key, value in {
                    "chapter": chapter,
                    "section": section,
                    "chapter_key": chapter_key,
                    "section_key": section_key,
                    "structure_path": structure_path,
                    "structure_source": metadata.get("structure_source"),
                    "structure_confidence": metadata.get("structure_confidence"),
                    "page_start": metadata.get("page_start"),
                    "page_end": metadata.get("page_end"),
                    "source_pages": metadata.get("source_pages"),
                }.items()
                if value not in (None, "", [])
            }
        )

        diagnostic_metadata = dict(metadata.get("diagnostic_metadata") or {})
        diagnostic_metadata.update(
            {
                key: value
                for key, value in {
                    "chapter": chapter,
                    "section": section,
                    "structure_path": structure_path,
                    "structure_source": metadata.get("structure_source"),
                    "structure_confidence": metadata.get("structure_confidence"),
                    "parser_backend": metadata.get("parser_backend"),
                    "parser_provenance": metadata.get("parser_provenance"),
                }.items()
                if value not in (None, "", [])
            }
        )

        ingestion_metadata = dict(metadata.get("ingestion_metadata") or {})
        ingestion_metadata.update(
            {
                key: value
                for key, value in {
                    "chapter_key": chapter_key,
                    "section_key": section_key,
                    "toc_page_offset": metadata.get("toc_page_offset"),
                }.items()
                if value not in (None, "", [])
            }
        )

        metadata["structure_path"] = structure_path
        if chapter_key:
            metadata["chapter_key"] = chapter_key
        if section_key:
            metadata["section_key"] = section_key
        if retrieval_metadata:
            metadata["retrieval_metadata"] = retrieval_metadata
        if diagnostic_metadata:
            metadata["diagnostic_metadata"] = diagnostic_metadata
        if ingestion_metadata:
            metadata["ingestion_metadata"] = ingestion_metadata
        return metadata

    def _build_question_bank_chunk(
        self,
        document: KnowledgeDocument,
        *,
        content: str,
        question_number: str,
        question_text: str,
        answer_text: str | None = None,
        explanation_text: str | None = None,
        asset_refs: list[dict[str, Any]] | None = None,
        chapter: str | None = None,
        section: str | None = None,
        tags: list[str] | None = None,
        structure_path: list[str] | None = None,
        source_format: str | None = None,
        source_locator: str | None = None,
        parser_backend: str | None = None,
        parser_provenance: dict[str, Any] | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        source_pages: list[int] | None = None,
        source_block_types: list[str] | None = None,
        raw_block_text: str | None = None,
    ) -> PreparedChunk:
        question_metadata = self.question_bank_post_processor.build_metadata(
            document,
            QuestionBankChunkCandidate(
                question_number=question_number,
                question_text=question_text,
                answer_text=answer_text,
                explanation_text=explanation_text,
                raw_block_text=raw_block_text,
                asset_refs=list(asset_refs or []),
                source_format=source_format,
                source_locator=source_locator,
                parser_backend=parser_backend,
                parser_provenance=parser_provenance,
                page_start=page_start,
                page_end=page_end,
                source_pages=list(source_pages or []),
                source_block_types=list(source_block_types or []),
                structure_path=list(structure_path or []),
            ),
        )
        if tags:
            question_metadata["tags"] = tags
        return PreparedChunk(
            content=content,
            metadata=self._build_chunk_metadata(
                document,
                chapter=chapter,
                section=section,
                extra_metadata=question_metadata,
            ),
        )

    def _prepare_question_chunks(
        self,
        document: KnowledgeDocument,
        text: str,
        asset_map: dict[str, ExtractedAsset],
        *,
        source_format: str | None = None,
        parser_backend: str | None = None,
        parser_provenance: dict[str, Any] | None = None,
    ) -> list[PreparedChunk]:
        normalized_text = self._normalize_question_source_text(text)
        question_text, answer_bank = self._split_question_and_answer_sections(normalized_text or text)
        question_blocks = self._parse_numbered_blocks(
            question_text,
            keep_wrapped_subquestions=True,
        )
        if not question_blocks:
            return []

        answer_lookup = self._parse_answer_bank(answer_bank or "")
        prepared_chunks: list[PreparedChunk] = []
        for number, block_text in question_blocks:
            question_body, local_answer, local_explanation = self._split_question_block_sections(block_text)
            merged_answer = local_answer or answer_lookup.get(number, {}).get("answer_text")
            merged_explanation = local_explanation or answer_lookup.get(number, {}).get("explanation_text")
            combined_text = self._compose_question_chunk_text(
                number=number,
                question_text=question_body,
                answer_text=merged_answer,
                explanation_text=merged_explanation,
            )
            finalized_text, asset_refs = self._finalize_chunk_text_and_assets(combined_text, asset_map)
            if not finalized_text.strip():
                continue
            clean_question_text, question_assets = self._finalize_chunk_text_and_assets(question_body, asset_map)
            clean_answer_text, _ = self._finalize_chunk_text_and_assets(merged_answer or "", asset_map)
            clean_explanation_text, _ = self._finalize_chunk_text_and_assets(merged_explanation or "", asset_map)
            if question_assets and not asset_refs:
                asset_refs = question_assets
            readable_question_text = self._normalize_question_readability_layout(
                clean_question_text or finalized_text
            )
            readable_answer_text = self._format_compound_judgement_answers(
                readable_question_text,
                clean_answer_text or None,
            )
            finalized_text = self._compose_question_chunk_text(
                number=number,
                question_text=readable_question_text,
                answer_text=readable_answer_text,
                explanation_text=clean_explanation_text or None,
            )
            prepared_chunks.append(
                self._build_question_bank_chunk(
                    document,
                    content=finalized_text,
                    question_number=number,
                    question_text=readable_question_text or finalized_text,
                    answer_text=readable_answer_text,
                    explanation_text=clean_explanation_text or None,
                    asset_refs=asset_refs,
                    source_format=source_format,
                    source_locator=f"question:{number}",
                    parser_backend=parser_backend,
                    parser_provenance=parser_provenance,
                    raw_block_text=block_text,
                )
            )
        return prepared_chunks

    def _normalize_question_source_text(self, text: str) -> str:
        raw_text = str(text or "").strip()
        if not raw_text:
            return ""
        raw_text = self._strip_office_text_style_artifacts(raw_text)
        normalized = self.pdf_parse_bridge._normalize_formula_text(raw_text)
        normalized = self._drop_residual_mineru_formula_markers(normalized)
        normalized = self.pdf_parse_bridge._strip_bound_asset_path_noise(
            normalized,
            asset_bound="[[asset:" in normalized,
        )
        normalized = self._normalize_docx_block_text(normalized)
        return self._normalize_question_readability_layout(normalized)

    def _drop_residual_mineru_formula_markers(self, text: str) -> str:
        lines = [line.rstrip() for line in str(text or "").splitlines()]
        cleaned: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            lowered = line.lower()
            if lowered not in {"equation_inline", "equation_display"}:
                cleaned.append(lines[index])
                index += 1
                continue
            next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if not next_line:
                index += 1
                continue
            if QUESTION_START_PATTERN.match(next_line):
                index += 1
                continue
            if self.pdf_parse_bridge._looks_like_formula_image_path(next_line):
                index += 2
                continue
            cleaned.append(next_line)
            index += 2
        return "\n".join(cleaned)

    def _normalize_question_readability_layout(self, text: str) -> str:
        normalized = str(text or "").replace("\t", "\n")
        normalized = re.sub(r"(?<!\n)(?=(?:[A-H]|[TtFf])\s*[．.、])", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _format_compound_judgement_answers(self, question_text: str, answer_text: str | None) -> str | None:
        if not answer_text:
            return None
        stripped = str(answer_text).strip()
        if not stripped or "\n" in stripped:
            return stripped or None
        markers = re.findall(r"[（(](\d{1,2})[)）]", str(question_text or ""))
        if len(markers) < 2:
            return stripped
        tokens = [token for token in re.split(r"\s+", stripped) if token]
        if len(tokens) != len(markers):
            return stripped
        if not all(token in {"正确", "错误", "对", "错", "√", "×", "T", "F"} for token in tokens):
            return stripped
        return "\n".join(f"（{marker}）{token}" for marker, token in zip(markers, tokens))

    def _strip_office_text_style_artifacts(self, text: str) -> str:
        normalized = str(text or "")
        if "<text" not in normalized.lower() and "</text>" not in normalized.lower() and 'style="' not in normalized.lower():
            return normalized.strip()
        normalized = re.sub(r'(?i)<+\s*/?\s*text(?:\s+style="[^"]*")?\s*>', "", normalized)
        normalized = re.sub(r"(?i)</\s*text\s*>", "", normalized)
        normalized = re.sub(r'(?i)(?:text|ext|xt)\s+style="[^"]*">', "", normalized)
        normalized = re.sub(r"(?i)<[^>\n]*text[^>\n]*>", "", normalized)
        normalized = normalized.replace("<", "").replace(">", "")
        return normalized.strip()

    def _split_question_and_answer_sections(self, text: str) -> tuple[str, str | None]:
        lines = [line.rstrip() for line in text.split("\n")]
        question_line_count = sum(1 for line in lines if self._looks_like_top_level_question_start(line))
        if question_line_count < 2:
            return text, None
        for index, line in enumerate(lines):
            if not QUESTION_SECTION_HEADING_PATTERN.match(line.strip()):
                continue
            head_lines = lines[:index]
            if any(
                ANSWER_LINE_PATTERN.match(item.strip()) or EXPLANATION_LINE_PATTERN.match(item.strip())
                for item in head_lines
                if item.strip()
            ):
                continue
            tail = "\n".join(lines[index + 1 :]).strip()
            if not tail:
                continue
            tail_question_count = sum(1 for item in tail.split("\n") if self._looks_like_top_level_question_start(item))
            if tail_question_count >= max(1, question_line_count // 3):
                head = "\n".join(head_lines).strip()
                if head:
                    return head, tail
        return text, None

    def _looks_like_top_level_question_start(self, text: str) -> bool:
        matched = QUESTION_START_PATTERN.match(str(text or "").strip())
        return bool(matched and (matched.group("ordinal") or matched.group("plain")))

    def _parse_numbered_blocks(
        self,
        text: str,
        *,
        keep_wrapped_subquestions: bool = False,
    ) -> list[tuple[str, str]]:
        lines = [line.rstrip() for line in text.split("\n")]
        blocks: list[tuple[str, list[str]]] = []
        current_number: str | None = None
        current_lines: list[str] = []
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                if current_lines:
                    current_lines.append("")
                continue
            if current_number and QUESTION_CATEGORY_HEADING_PATTERN.match(stripped):
                if any(item.strip() for item in current_lines):
                    blocks.append((current_number, current_lines[:]))
                current_number = None
                current_lines = []
                continue
            matched = QUESTION_START_PATTERN.match(stripped)
            if matched and not (
                keep_wrapped_subquestions and matched.group("wrapped") and current_number
            ):
                if current_number and any(item.strip() for item in current_lines):
                    blocks.append((current_number, current_lines[:]))
                current_number = self._question_number_from_match(matched)
                body = matched.group("body") or ""
                current_lines = [body.strip()] if body.strip() else []
                continue
            if current_number:
                current_lines.append(stripped)
        if current_number and any(item.strip() for item in current_lines):
            blocks.append((current_number, current_lines[:]))
        return [(number, "\n".join(lines).strip()) for number, lines in blocks if "\n".join(lines).strip()]

    def _question_number_from_match(self, matched: re.Match[str]) -> str:
        return matched.group("ordinal") or matched.group("plain") or matched.group("wrapped") or ""

    def _parse_answer_bank(self, text: str) -> dict[str, dict[str, str]]:
        answer_blocks = self._parse_numbered_blocks(text)
        if not answer_blocks:
            return {}
        answer_lookup: dict[str, dict[str, str]] = {}
        for number, block_text in answer_blocks:
            answer_text, explanation_text = self._split_answer_block_sections(block_text)
            answer_lookup[number] = {
                "answer_text": answer_text or "",
                "explanation_text": explanation_text or "",
            }
        return answer_lookup

    def _split_question_block_sections(self, text: str) -> tuple[str, str | None, str | None]:
        question_lines: list[str] = []
        answer_lines: list[str] = []
        explanation_lines: list[str] = []
        current_section = "question"
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            answer_match = ANSWER_LINE_PATTERN.match(stripped)
            explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
            if DIFFICULTY_LINE_PATTERN.match(stripped) or KNOWLEDGE_POINTS_LINE_PATTERN.match(stripped):
                continue
            if answer_match:
                current_section = "answer"
                inline_body = (answer_match.group("body") or "").strip()
                if inline_body:
                    answer_lines.append(inline_body)
                continue
            if explanation_match:
                current_section = "explanation"
                inline_body = (explanation_match.group("body") or "").strip()
                if inline_body:
                    explanation_lines.append(inline_body)
                continue
            if current_section == "question":
                question_lines.append(stripped)
            elif current_section == "answer":
                answer_lines.append(stripped)
            else:
                explanation_lines.append(stripped)
        return (
            "\n".join(question_lines).strip(),
            "\n".join(answer_lines).strip() or None,
            "\n".join(explanation_lines).strip() or None,
        )

    def _split_answer_block_sections(self, text: str) -> tuple[str | None, str | None]:
        answer_lines: list[str] = []
        explanation_lines: list[str] = []
        current_section = "answer"
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
            answer_match = ANSWER_LINE_PATTERN.match(stripped)
            if DIFFICULTY_LINE_PATTERN.match(stripped) or KNOWLEDGE_POINTS_LINE_PATTERN.match(stripped):
                continue
            if explanation_match:
                current_section = "explanation"
                inline_body = (explanation_match.group("body") or "").strip()
                if inline_body:
                    explanation_lines.append(inline_body)
                continue
            if answer_match:
                current_section = "answer"
                inline_body = (answer_match.group("body") or "").strip()
                if inline_body:
                    answer_lines.append(inline_body)
                continue
            if current_section == "answer":
                answer_lines.append(stripped)
            else:
                explanation_lines.append(stripped)
        return "\n".join(answer_lines).strip() or None, "\n".join(explanation_lines).strip() or None

    def _compose_question_chunk_text(
        self,
        *,
        number: str,
        question_text: str,
        answer_text: str | None,
        explanation_text: str | None,
    ) -> str:
        parts = [f"第{number}题", f"题目：\n{question_text.strip()}"]
        if answer_text:
            parts.append(f"答案：\n{answer_text.strip()}")
        if explanation_text:
            parts.append(f"解析：\n{explanation_text.strip()}")
        return "\n\n".join(part for part in parts if part.strip()).strip()

    def _finalize_chunk_text_and_assets(
        self,
        text: str,
        asset_map: dict[str, ExtractedAsset],
    ) -> tuple[str, list[dict[str, Any]]]:
        if not text:
            return "", []
        asset_refs: list[dict[str, Any]] = []
        seen_asset_ids: set[str] = set()

        def replace_asset(match: re.Match[str]) -> str:
            asset_id = match.group(1)
            asset = asset_map.get(asset_id)
            if not asset:
                return "【附图】"
            if asset.asset_id not in seen_asset_ids:
                seen_asset_ids.add(asset.asset_id)
                asset_refs.append(self._asset_payload(asset))
            label = f"【附图{len(asset_refs)}"
            if asset.title:
                label += f"：{asset.title}"
            label += "】"
            return label

        replaced = ASSET_MARKER_PATTERN.sub(replace_asset, text)
        normalized = self._normalize_docx_block_text(replaced)
        return normalized, asset_refs

    def _asset_payload(self, asset: ExtractedAsset) -> dict[str, Any]:
        return {
            "asset_id": asset.asset_id,
            "filename": asset.filename,
            "content_type": asset.content_type,
            "url": asset.public_url,
            "title": asset.title,
            "description": asset.description,
        }

    def _segment_text_with_context(self, text: str, document: KnowledgeDocument) -> list[dict[str, Any]]:
        paragraphs = [paragraph.strip() for paragraph in text.split("\n") if paragraph.strip()]
        if not paragraphs:
            return []

        current_chapter = document.chapter
        current_section = document.section
        segments: list[dict[str, Any]] = []
        current_lines: list[str] = []
        found_heading = False

        def flush_segment() -> None:
            if not current_lines:
                return
            body = "\n".join(current_lines).strip()
            if body:
                segments.append(
                    {
                        "text": body,
                        "chapter": current_chapter,
                        "section": current_section,
                    }
                )
            current_lines.clear()

        for paragraph in paragraphs:
            heading_context = self._extract_heading_context(paragraph, document.resource_type)
            if heading_context["chapter"]:
                found_heading = True
                flush_segment()
                current_chapter = heading_context["chapter"]
                current_section = None
                current_lines.append(paragraph)
                continue
            if heading_context["section"]:
                found_heading = True
                flush_segment()
                current_section = heading_context["section"]
                current_lines.append(paragraph)
                continue
            current_lines.append(paragraph)

        flush_segment()
        if segments and found_heading:
            return segments
        return [
            {
                "text": "\n".join(paragraphs),
                "chapter": document.chapter,
                "section": document.section,
            }
        ]

    def _extract_heading_context(self, text: str, resource_type: str | None) -> dict[str, str | None]:
        if resource_type not in CHAPTER_AWARE_RESOURCE_TYPES:
            return {"chapter": None, "section": None}
        section_patterns = SECTION_HEADING_PATTERNS
        for line in [item.strip() for item in text.split("\n") if item.strip()][:3]:
            if line.endswith("。") or len(line) > 48:
                continue
            if resource_type == ResourceType.TEXTBOOK.value and line in TEXTBOOK_BACK_MATTER_HEADINGS:
                return {"chapter": line[:255], "section": None}
            for pattern in CHAPTER_HEADING_PATTERNS:
                if pattern.match(line):
                    return {"chapter": line[:255], "section": None}
            if self._looks_like_question_heading(line):
                continue
            if resource_type == ResourceType.TEXTBOOK.value and self._looks_like_sentence_style_textbook_line(line):
                continue
            for pattern in section_patterns:
                if pattern.match(line):
                    return {"chapter": None, "section": line[:255]}
        return {"chapter": None, "section": None}

    def _looks_like_question_heading(self, line: str) -> bool:
        normalized = str(line or "").strip()
        if not normalized:
            return False
        return any(token in normalized for token in QUESTION_LIKE_HEADING_HINTS)

    def _looks_like_sentence_style_textbook_line(self, line: str) -> bool:
        normalized = str(line or "").strip()
        if not normalized:
            return False
        if any(token in normalized for token in TEXTBOOK_SENTENCE_PUNCTUATION):
            return True
        if any(token in normalized for token in ("“", "”", "\"", "‘", "’")):
            return True
        body = re.sub(r"^\s*(?:[（(]?\d+[)）]?|第\s*\d+\s*题|\d+\s*[.．、:：)])\s*", "", normalized)
        if len(body.strip()) <= 3:
            return True
        return False

    def _is_ambiguous_textbook_section_heading(self, line: str) -> bool:
        normalized = str(line or "").strip()
        return bool(GENERIC_SINGLE_LEVEL_SECTION_PATTERN.match(normalized)) and not bool(
            DECIMAL_SECTION_HEADING_PATTERN.match(normalized)
        )

    def _structure_key(self, text: str | None) -> str | None:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return None
        normalized = (
            normalized.replace("（", "(")
            .replace("）", ")")
            .replace("：", ":")
            .replace("－", "-")
            .replace("—", "-")
            .replace("–", "-")
            .replace("．", ".")
            .replace("、", ".")
            .replace("　", " ")
        )
        key = re.sub(r"[\s\-\.:：·•⋯…()（）【】\[\]<>《》]+", "", normalized)
        return key or None

    def _retrieval_structure(self, metadata: dict[str, Any], document: KnowledgeDocument | None = None) -> dict[str, Any]:
        retrieval = metadata.get("retrieval_metadata")
        retrieval_metadata = dict(retrieval) if isinstance(retrieval, dict) else {}
        if document is not None:
            if not retrieval_metadata.get("chapter") and document.chapter:
                retrieval_metadata["chapter"] = document.chapter
            if not retrieval_metadata.get("section") and document.section:
                retrieval_metadata["section"] = document.section
        if not retrieval_metadata.get("chapter") and metadata.get("chapter"):
            retrieval_metadata["chapter"] = metadata.get("chapter")
        if not retrieval_metadata.get("section") and metadata.get("section"):
            retrieval_metadata["section"] = metadata.get("section")
        structure_path = retrieval_metadata.get("structure_path")
        if not isinstance(structure_path, list):
            structure_path = metadata.get("structure_path")
        if not isinstance(structure_path, list):
            structure_path = [
                item for item in (retrieval_metadata.get("chapter"), retrieval_metadata.get("section")) if str(item or "").strip()
            ]
        retrieval_metadata["structure_path"] = [str(item).strip() for item in structure_path if str(item).strip()]
        if not retrieval_metadata.get("chapter_key"):
            retrieval_metadata["chapter_key"] = metadata.get("chapter_key") or self._structure_key(retrieval_metadata.get("chapter"))
        if not retrieval_metadata.get("section_key"):
            retrieval_metadata["section_key"] = metadata.get("section_key") or self._structure_key(retrieval_metadata.get("section"))
        if not retrieval_metadata.get("structure_source") and metadata.get("structure_source"):
            retrieval_metadata["structure_source"] = metadata.get("structure_source")
        if not retrieval_metadata.get("structure_confidence") and metadata.get("structure_confidence"):
            retrieval_metadata["structure_confidence"] = metadata.get("structure_confidence")
        return retrieval_metadata

    def _infer_question_profile(self, question: str) -> QuestionProfile:
        lowered = question.strip()
        question_type = "concept"
        preferred_resources = [
            ResourceType.KNOWLEDGE_NOTE.value,
            ResourceType.TEXTBOOK.value,
            ResourceType.EXERCISE.value,
            ResourceType.QUESTION_SET.value,
            ResourceType.EXTENSION.value,
        ]
        if any(keyword in lowered for keyword in ["求", "计算", "解", "证明", "推导", "例题", "题目", "真题", "试卷"]):
            question_type = "calculation"
            preferred_resources = [
                ResourceType.EXERCISE.value,
                ResourceType.QUESTION_SET.value,
                ResourceType.KNOWLEDGE_NOTE.value,
                ResourceType.TEXTBOOK.value,
                ResourceType.EXTENSION.value,
            ]
        elif any(keyword in lowered for keyword in ["分析", "评价", "说明原因", "材料", "比较"]):
            question_type = "analysis"
            preferred_resources = [
                ResourceType.KNOWLEDGE_NOTE.value,
                ResourceType.TEXTBOOK.value,
                ResourceType.EXTENSION.value,
                ResourceType.EXERCISE.value,
                ResourceType.QUESTION_SET.value,
            ]

        prefer_extension = any(keyword in lowered for keyword in ["物理学史", "生活中的", "拓展", "科学家", "历史", "应用"])
        if prefer_extension:
            preferred_resources = [
                ResourceType.EXTENSION.value,
                *[item for item in preferred_resources if item != ResourceType.EXTENSION.value],
            ]

        desired_difficulty = None
        if any(keyword in lowered for keyword in ["基础", "简单", "入门"]):
            desired_difficulty = DifficultyLevel.BASIC.value
        elif any(keyword in lowered for keyword in ["提高", "综合", "压轴", "竞赛", "难"]):
            desired_difficulty = DifficultyLevel.ADVANCED.value
        elif question_type == "calculation":
            desired_difficulty = DifficultyLevel.STANDARD.value

        return QuestionProfile(
            question_type=question_type,
            preferred_resources=preferred_resources,
            desired_difficulty=desired_difficulty,
            prefer_extension=prefer_extension,
        )

    def _rerank_rows(
        self,
        *,
        question: str,
        profile: QuestionProfile,
        scored_rows: list[tuple[float, KnowledgeChunk]],
        student_grade: int | None,
    ) -> list[KnowledgeChunk]:
        rescored: list[tuple[float, KnowledgeChunk]] = []
        for base_score, row in scored_rows:
            final_score = base_score + self._metadata_score(
                row,
                question,
                profile,
                student_grade,
                recommendation_mode=False,
            )
            rescored.append((final_score, row))
        rescored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in rescored[: self.settings.rag_top_k]]

    def _metadata_score(
        self,
        row: KnowledgeChunk,
        question: str,
        profile: QuestionProfile,
        student_grade: int | None,
        *,
        recommendation_mode: bool,
    ) -> float:
        score = 0.0
        document = row.document
        if not document:
            return score

        resource_type = document.resource_type or ResourceType.KNOWLEDGE_NOTE.value
        if resource_type in profile.preferred_resources:
            score += max(0.04, 0.26 - profile.preferred_resources.index(resource_type) * 0.05)
        if profile.prefer_extension and resource_type == ResourceType.EXTENSION.value:
            score += 0.18

        if profile.desired_difficulty and document.difficulty and resource_type in QUESTION_RESOURCE_TYPES:
            if document.difficulty == profile.desired_difficulty:
                score += 0.14
            elif {
                profile.desired_difficulty,
                document.difficulty,
            } <= {DifficultyLevel.BASIC.value, DifficultyLevel.STANDARD.value}:
                score += 0.06

        metadata = row.metadata_json or {}
        retrieval_metadata = self._retrieval_structure(metadata, document)
        question_lowered = question.lower()
        question_key = self._structure_key(question_lowered) or ""
        chapter = str(retrieval_metadata.get("chapter") or "").strip()
        section = str(retrieval_metadata.get("section") or "").strip()
        chapter_key = str(retrieval_metadata.get("chapter_key") or "").strip()
        section_key = str(retrieval_metadata.get("section_key") or "").strip()
        structure_path = [str(item).strip() for item in retrieval_metadata.get("structure_path") or [] if str(item).strip()]

        if chapter and chapter.lower() in question_lowered:
            score += 0.22
        elif chapter_key and chapter_key in question_key:
            score += 0.22
        if section and section.lower() in question_lowered:
            score += 0.16
        elif section_key and section_key in question_key:
            score += 0.16
        if len(structure_path) >= 2 and all(self._structure_key(item) and self._structure_key(item) in question_key for item in structure_path[:2]):
            score += 0.06
        if retrieval_metadata.get("structure_source") == "body_heading_normalized":
            score += 0.03
        elif retrieval_metadata.get("structure_source") == "toc_page_map":
            score += 0.01
        score += self._grade_match_score(row, student_grade, recommendation_mode=recommendation_mode)
        for tag in document.tags[:5]:
            if tag.lower() in question_lowered:
                score += 0.08
        return score

    def _recommendation_profile(self, question: str) -> QuestionProfile:
        base_profile = self._infer_question_profile(question)
        return QuestionProfile(
            question_type=base_profile.question_type,
            preferred_resources=[ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value],
            desired_difficulty=base_profile.desired_difficulty,
            prefer_extension=False,
        )

    def _is_question_row(self, row: KnowledgeChunk) -> bool:
        return self._question_row_tier(row) is not None

    def _question_row_tier(self, row: KnowledgeChunk) -> str | None:
        document = row.document
        if not document:
            return None
        resource_type = document.resource_type or ResourceType.KNOWLEDGE_NOTE.value
        if resource_type not in QUESTION_RESOURCE_TYPES:
            return None
        metadata = row.metadata_json or {}
        chunk_kind = metadata.get("chunk_kind")
        if chunk_kind == "question_item":
            return "preferred"
        if chunk_kind in {None, ""} and str(metadata.get("question_text") or row.content or "").strip():
            return "fallback"
        return None

    def _score_question_rows(
        self,
        rows: list[KnowledgeChunk],
        question: str,
        student_grade: int | None,
        query_embedding: list[float],
        profile: QuestionProfile,
    ) -> list[tuple[float, KnowledgeChunk]]:
        if not rows:
            return []
        candidate_texts = [self._question_candidate_text(row) for row in rows]
        candidate_embeddings = self.embedder.embed_texts(candidate_texts)
        scored_rows: list[tuple[float, KnowledgeChunk]] = []
        for row, candidate_text, candidate_embedding in zip(rows, candidate_texts, candidate_embeddings):
            score = self.embedder.cosine_similarity(query_embedding, candidate_embedding)
            score += sum(1 for char in question[:16] if char and char in candidate_text) / 24.0
            score += self._metadata_score(row, question, profile, student_grade, recommendation_mode=True)
            score += self._question_recommendation_bonus(row, question)
            scored_rows.append((score, row))
        scored_rows.sort(key=lambda item: item[0], reverse=True)
        return scored_rows

    def _grade_match_score(self, row: KnowledgeChunk, student_grade: int | None, *, recommendation_mode: bool) -> float:
        if student_grade is None or student_grade not in HIGH_SCHOOL_GRADE_LABELS:
            return 0.0

        document = row.document
        if not document:
            return 0.0

        metadata = row.metadata_json or {}
        signal_grades: set[int] = set()
        exact_matches = 0

        document_grade = document.grade
        if document_grade == student_grade:
            exact_matches += 1
        elif document_grade in HIGH_SCHOOL_GRADE_LABELS:
            signal_grades.add(document_grade)

        metadata_grade = metadata.get("grade")
        if isinstance(metadata_grade, int):
            if metadata_grade == student_grade:
                exact_matches += 1
            elif metadata_grade in HIGH_SCHOOL_GRADE_LABELS:
                signal_grades.add(metadata_grade)

        tag_candidates = list(document.tags)
        metadata_tags = metadata.get("tags")
        if isinstance(metadata_tags, list):
            tag_candidates.extend(str(tag) for tag in metadata_tags if str(tag).strip())
        tag_grades = extract_grade_levels(tag_candidates)
        if student_grade in tag_grades:
            exact_matches += 1
        elif tag_grades:
            signal_grades.update(tag_grades)

        if exact_matches:
            base_bonus = 0.34 if not recommendation_mode else 0.52
            corroboration_bonus = 0.12 if not recommendation_mode else 0.24
            return base_bonus + max(0, exact_matches - 1) * corroboration_bonus
        if signal_grades:
            return -0.10 if not recommendation_mode else -0.18
        return 0.0

    def _question_candidate_text(self, row: KnowledgeChunk) -> str:
        metadata = row.metadata_json or {}
        parts = [str(metadata.get("question_text") or row.content)]
        retrieval_metadata = self._retrieval_structure(metadata, row.document)
        chapter = str(retrieval_metadata.get("chapter") or "").strip()
        section = str(retrieval_metadata.get("section") or "").strip()
        if chapter:
            parts.append(chapter)
        if section:
            parts.append(section)
        structure_path = [str(item).strip() for item in retrieval_metadata.get("structure_path") or [] if str(item).strip()]
        if structure_path:
            parts.append(" > ".join(structure_path[:3]))
        if metadata.get("contains_images"):
            parts.append("含图题")
        return "\n".join(part for part in parts if part).strip()

    def _question_row_key(self, row: KnowledgeChunk) -> tuple[int, str]:
        metadata = row.metadata_json or {}
        return row.document_id, str(metadata.get("question_uid") or metadata.get("source_locator") or metadata.get("question_number") or row.chunk_index)

    def _question_recommendation_bonus(self, row: KnowledgeChunk, question: str) -> float:
        metadata = row.metadata_json or {}
        score = 0.0
        if metadata.get("chunk_kind") == "question_item":
            score += 0.22
        else:
            score -= 0.05
        if metadata.get("answer_text"):
            score += 0.03
        if metadata.get("explanation_text"):
            score += 0.03
        if metadata.get("contains_images") and any(token in question for token in ["图", "图示", "模型", "装置", "几何体", "受力图", "电路图"]):
            score += 0.16
        if metadata.get("image_binding_status") == "missing_required":
            score -= 0.08
        return score

    def _format_context(self, rows: list[KnowledgeChunk]) -> str:
        parts: list[str] = []
        for index, row in enumerate(rows, start=1):
            labels = [f"资料片段 {index}"]
            document = row.document
            if document:
                labels.extend(self._chunk_labels(document, row))
            parts.append(f"[{' | '.join(labels)}] {row.content}")
        return "\n\n".join(parts)

    def _chunk_labels(self, document: KnowledgeDocument, row: KnowledgeChunk) -> list[str]:
        metadata = row.metadata_json or {}
        retrieval_metadata = self._retrieval_structure(metadata, document)
        labels = [self._resource_type_label(document.resource_type or ResourceType.KNOWLEDGE_NOTE.value)]
        grade = metadata.get("grade") or document.grade
        chapter = retrieval_metadata.get("chapter") or document.chapter
        section = retrieval_metadata.get("section") or document.section
        difficulty = metadata.get("difficulty") or document.difficulty
        question_number = metadata.get("question_number")
        if grade:
            labels.append(HIGH_SCHOOL_GRADE_LABELS.get(int(grade), f"{grade}年级"))
        if chapter:
            labels.append(str(chapter))
        if section:
            labels.append(str(section))
        if difficulty:
            labels.append(f"难度:{self._difficulty_label(str(difficulty))}")
        if question_number:
            labels.append(f"第{question_number}题")
        page_start = metadata.get("page_start")
        page_end = metadata.get("page_end")
        if page_start and page_end:
            labels.append(f"p{page_start}" if page_start == page_end else f"p{page_start}-{page_end}")
        if metadata.get("contains_images"):
            labels.append("含图片")
        return labels

    def _resource_type_label(self, value: str) -> str:
        mapping = {
            ResourceType.KNOWLEDGE_NOTE.value: "知识讲义",
            ResourceType.TEXTBOOK.value: "教材",
            ResourceType.EXERCISE.value: "习题例题",
            ResourceType.QUESTION_SET.value: "题库试卷",
            ResourceType.EXTENSION.value: "拓展资料",
        }
        return mapping.get(value, "资料")

    def _difficulty_label(self, value: str) -> str:
        mapping = {
            DifficultyLevel.BASIC.value: "基础",
            DifficultyLevel.STANDARD.value: "标准",
            DifficultyLevel.ADVANCED.value: "提高",
            DifficultyLevel.CHALLENGE.value: "挑战",
        }
        return mapping.get(value, value)

    def _extract_docx_text(self, file_path: str) -> str:
        return self._extract_docx_content(file_path).text

    def _extract_docx_content(self, file_path: str, *, document_id: int | None = None) -> ExtractionResult:
        try:
            with zipfile.ZipFile(file_path) as archive:
                document_xml = archive.read("word/document.xml")
                relationships = self._load_docx_relationships(archive)
                asset_dir: Path | None = None
                if document_id is not None:
                    self.clear_document_artifacts(document_id)
                    asset_dir = self.document_asset_dir(document_id)
                    asset_dir.mkdir(parents=True, exist_ok=True)
                context: dict[str, Any] = {
                    "archive": archive,
                    "relationships": relationships,
                    "asset_dir": asset_dir,
                    "assets": [],
                    "asset_cache": {},
                    "document_id": document_id,
                }
                return self._parse_docx_document(document_xml, context)
        except KeyError as exc:
            raise RuntimeError("DOCX 文件缺少 word/document.xml，无法解析正文内容") from exc
        except zipfile.BadZipFile as exc:
            raise RuntimeError("DOCX 文件损坏或格式不正确") from exc

    def _parse_docx_document(self, document_xml: bytes, context: dict[str, Any]) -> ExtractionResult:
        try:
            root = ET.fromstring(document_xml)
        except ET.ParseError as exc:
            raise RuntimeError("DOCX 文档 XML 解析失败") from exc

        body = root.find(f".//{{{DOCX_WORD_NS}}}body")
        if body is None:
            return ExtractionResult(text="")

        blocks: list[str] = []
        for child in body:
            local_name = self._xml_local_name(child.tag)
            if local_name == "p":
                text = self._docx_paragraph_text(child, context)
            elif local_name == "tbl":
                text = self._docx_table_text(child, context)
            else:
                text = self._docx_node_text(child, context)
            normalized = self._normalize_docx_block_text(text)
            if normalized:
                blocks.append(normalized)
        return ExtractionResult(text="\n".join(blocks).strip(), assets=list(context["assets"]))

    def _load_docx_relationships(self, archive: zipfile.ZipFile) -> dict[str, str]:
        try:
            rels_xml = archive.read("word/_rels/document.xml.rels")
        except KeyError:
            return {}
        try:
            root = ET.fromstring(rels_xml)
        except ET.ParseError:
            return {}
        relationships: dict[str, str] = {}
        for child in root:
            if self._xml_local_name(child.tag) != "Relationship":
                continue
            relation_id = child.attrib.get("Id")
            target = child.attrib.get("Target")
            if not relation_id or not target:
                continue
            relationships[relation_id] = self._resolve_docx_target_path("word/document.xml", target)
        return relationships

    def _resolve_docx_target_path(self, source_path: str, target: str) -> str:
        base_dir = PurePosixPath(source_path).parent
        target_path = PurePosixPath(target.lstrip("/")) if target.startswith("/") else (base_dir / PurePosixPath(target))
        return posixpath.normpath(str(target_path))

    def _docx_paragraph_text(self, paragraph: ET.Element, context: dict[str, Any]) -> str:
        return self._normalize_docx_block_text("".join(self._docx_node_text(child, context) for child in paragraph))

    def _docx_table_text(self, table: ET.Element, context: dict[str, Any]) -> str:
        rows: list[str] = []
        for row in table:
            if self._xml_local_name(row.tag) != "tr":
                continue
            cells: list[str] = []
            for cell in row:
                if self._xml_local_name(cell.tag) != "tc":
                    continue
                cell_blocks: list[str] = []
                for child in cell:
                    local_name = self._xml_local_name(child.tag)
                    if local_name == "p":
                        block_text = self._docx_paragraph_text(child, context)
                    elif local_name == "tbl":
                        block_text = self._docx_table_text(child, context)
                    else:
                        block_text = self._docx_node_text(child, context)
                    normalized = self._normalize_docx_block_text(block_text)
                    if normalized:
                        cell_blocks.append(normalized)
                cell_text = "\n".join(cell_blocks).strip()
                if cell_text:
                    cells.append(cell_text)
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows).strip()

    def _docx_node_text(self, element: ET.Element, context: dict[str, Any]) -> str:
        local_name = self._xml_local_name(element.tag)
        namespace = self._xml_namespace(element.tag)
        if namespace == DOCX_MATH_NS and local_name in {"oMath", "oMathPara"}:
            return self._omml_to_latex(element)
        if namespace == DOCX_WORD_NS and local_name == "object":
            return self._docx_ole_object_text(element, context)
        if namespace == DOCX_WORD_NS and local_name in {"drawing", "pict"}:
            return self._docx_image_marker(element, context)

        if namespace in {DOCX_WORD_NS, DOCX_MATH_NS} and local_name in {"t", "delText", "instrText"}:
            return element.text or ""
        if namespace == DOCX_WORD_NS and local_name == "tab":
            return "\t"
        if namespace == DOCX_WORD_NS and local_name in {"br", "cr"}:
            return "\n"

        parts: list[str] = []
        if element.text and element.text.strip():
            parts.append(element.text)
        for child in element:
            parts.append(self._docx_node_text(child, context))
            if child.tail and child.tail.strip():
                parts.append(child.tail)
        return "".join(parts)

    def _docx_ole_object_text(self, element: ET.Element, context: dict[str, Any]) -> str:
        formula_text = self._docx_extract_ole_formula_text(element, context)
        if formula_text:
            wrapped = self._wrap_formula(formula_text, display=False)
            return f" {wrapped} " if wrapped else ""
        fallback = self._docx_ole_object_fallback_label(element)
        if fallback:
            return f" {fallback} "
        return ""

    def _docx_extract_ole_formula_text(self, element: ET.Element, context: dict[str, Any]) -> str | None:
        embed_id = None
        prog_id = None
        for descendant in element.iter():
            if self._xml_namespace(descendant.tag) == DOCX_OLE_NS and self._xml_local_name(descendant.tag) == "OLEObject":
                embed_id = self._xml_attr(descendant, "id") or embed_id
                prog_id = descendant.attrib.get("ProgID") or prog_id
                break
        if not embed_id or not prog_id or prog_id != "Equation.DSMT4":
            return None
        target_path = context["relationships"].get(embed_id)
        if not target_path:
            return None
        archive: zipfile.ZipFile = context["archive"]
        try:
            payload = archive.read(target_path)
        except KeyError:
            return None
        return self._extract_legacy_equation_text(payload)

    def _docx_ole_object_fallback_label(self, element: ET.Element) -> str | None:
        for descendant in element.iter():
            if self._xml_namespace(descendant.tag) == DOCX_VML_NS and self._xml_local_name(descendant.tag) == "shape":
                alt = descendant.attrib.get("alt")
                if alt:
                    return "【公式对象】"
            if self._xml_namespace(descendant.tag) == DOCX_VML_NS and self._xml_local_name(descendant.tag) == "imagedata":
                title = self._xml_attr(descendant, "title")
                if title:
                    return "【公式对象】"
        return "【公式对象】"

    def _extract_legacy_equation_text(self, payload: bytes) -> str | None:
        stream = self._extract_ole_stream(payload, "Equation Native")
        if not stream:
            return None
        tex_text = self._extract_equation_tex_text(stream)
        if tex_text:
            return tex_text
        return self._extract_equation_char_fallback(stream)

    def _extract_ole_stream(self, payload: bytes, stream_name: str) -> bytes | None:
        if len(payload) < 512 or not payload.startswith(OLE_CF_MAGIC):
            return None
        try:
            sector_size = 1 << self._ole_u16(payload, 30)
            mini_sector_size = 1 << self._ole_u16(payload, 32)
            directory_start = self._ole_u32(payload, 48)
            mini_stream_cutoff = self._ole_u32(payload, 56)
            mini_fat_start = self._ole_u32(payload, 60)
            difat = [
                self._ole_u32(payload, 76 + index * OLE_UINT32_SIZE)
                for index in range(109)
            ]
            fat: list[int] = []
            for sector in [item for item in difat if item != OLE_FREE_SECTOR]:
                offset = 512 + sector * sector_size
                fat.extend(
                    struct.unpack(
                        "<" + "I" * (sector_size // OLE_UINT32_SIZE),
                        payload[offset : offset + sector_size],
                    )
                )
            directory_bytes = b"".join(
                payload[512 + sector * sector_size : 512 + (sector + 1) * sector_size]
                for sector in self._ole_sector_chain(fat, directory_start)
            )
            entries: dict[str, tuple[int, int, int]] = {}
            for index in range(0, len(directory_bytes), 128):
                entry = directory_bytes[index : index + 128]
                name_length = self._ole_u16(entry, 64)
                if name_length < 2:
                    continue
                name = entry[: name_length - 2].decode("utf-16le", "ignore")
                entries[name] = (
                    entry[66],
                    self._ole_u32(entry, 116),
                    struct.unpack_from("<Q", entry, 120)[0],
                )
            if "Root Entry" not in entries or stream_name not in entries:
                return None
            mini_fat: list[int] = []
            for sector in self._ole_sector_chain(fat, mini_fat_start):
                offset = 512 + sector * sector_size
                mini_fat.extend(
                    struct.unpack(
                        "<" + "I" * (sector_size // OLE_UINT32_SIZE),
                        payload[offset : offset + sector_size],
                    )
                )
            _, root_start, root_size = entries["Root Entry"]
            root_stream = b"".join(
                payload[512 + sector * sector_size : 512 + (sector + 1) * sector_size]
                for sector in self._ole_sector_chain(fat, root_start)
            )[:root_size]
            _, stream_start, stream_size = entries[stream_name]
            if stream_size < mini_stream_cutoff:
                chunks: list[bytes] = []
                sector = stream_start
                seen: set[int] = set()
                while sector not in {OLE_END_OF_CHAIN, OLE_FREE_SECTOR} and sector not in seen:
                    seen.add(sector)
                    offset = sector * mini_sector_size
                    chunks.append(root_stream[offset : offset + mini_sector_size])
                    sector = mini_fat[sector]
                return b"".join(chunks)[:stream_size]
            return b"".join(
                payload[512 + sector * sector_size : 512 + (sector + 1) * sector_size]
                for sector in self._ole_sector_chain(fat, stream_start)
            )[:stream_size]
        except (IndexError, KeyError, struct.error, ValueError):
            return None

    def _ole_sector_chain(self, fat: list[int], start_sector: int) -> list[int]:
        chain: list[int] = []
        seen: set[int] = set()
        sector = start_sector
        while sector not in {OLE_END_OF_CHAIN, OLE_FREE_SECTOR} and sector not in seen:
            if sector < 0 or sector >= len(fat):
                break
            seen.add(sector)
            chain.append(sector)
            sector = fat[sector]
        return chain

    def _ole_u32(self, buffer: bytes, offset: int) -> int:
        return struct.unpack_from("<I", buffer, offset)[0]

    def _ole_u16(self, buffer: bytes, offset: int) -> int:
        return struct.unpack_from("<H", buffer, offset)[0]

    def _extract_equation_tex_text(self, stream: bytes) -> str | None:
        decoded = stream.decode("latin1", "ignore")
        matched = re.search(r"TeX Input Language\x00([^\x00]{1,200})\x00", decoded)
        if not matched:
            return None
        return self._normalize_legacy_formula_text(matched.group(1))

    def _extract_equation_char_fallback(self, stream: bytes) -> str | None:
        printable = []
        for byte in stream:
            if 32 <= byte < 127:
                printable.append(chr(byte))
            else:
                printable.append("\n")
        lines = [line.strip() for line in re.sub(r"\n+", "\n", "".join(printable)).split("\n") if line.strip()]
        if not lines:
            return None
        filtered_lines = [line for line in lines if line not in OLE_METADATA_STRINGS]
        if not filtered_lines:
            return None
        tail: list[str] = []
        for line in reversed(filtered_lines):
            if len(line) == 1 and re.fullmatch(r"[A-Za-z0-9=+\-*/()\\]", line):
                tail.append(line)
                continue
            break
        if tail:
            return self._normalize_legacy_formula_text("".join(reversed(tail)))
        candidates = [
            line
            for line in filtered_lines
            if re.search(r"[A-Za-z0-9]", line)
            and any(token in line for token in ("\\", "=", "(", ")", "+", "-", "*", "/"))
        ]
        if candidates:
            return self._normalize_legacy_formula_text(candidates[-1])
        if len(filtered_lines[-1]) <= 16 and re.search(r"[A-Za-z0-9]", filtered_lines[-1]):
            return self._normalize_legacy_formula_text(filtered_lines[-1])
        return None

    def _normalize_legacy_formula_text(self, text: str) -> str | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        normalized = normalized.replace("\xa0", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"(?<=[A-Za-z])==(?=[A-Za-z0-9])", "=", normalized)
        normalized = normalized.replace("\\\\", "\\")
        return normalized.strip() or None

    def _docx_image_marker(self, element: ET.Element, context: dict[str, Any]) -> str:
        embed_id = self._docx_embed_id(element)
        if not embed_id:
            return ""
        asset = self._docx_extract_asset(embed_id, element, context)
        if not asset:
            return ""
        return f" [[asset:{asset.asset_id}]] "

    def _docx_embed_id(self, element: ET.Element) -> str | None:
        for descendant in element.iter():
            if self._xml_local_name(descendant.tag) == "blip":
                embed_id = self._xml_attr(descendant, "embed") or self._xml_attr(descendant, "link")
                if embed_id:
                    return embed_id
            if self._xml_local_name(descendant.tag) == "imagedata":
                embed_id = self._xml_attr(descendant, "id")
                if embed_id:
                    return embed_id
        return None

    def _docx_extract_asset(
        self,
        embed_id: str,
        element: ET.Element,
        context: dict[str, Any],
    ) -> ExtractedAsset | None:
        cached = context["asset_cache"].get(embed_id)
        if cached:
            return cached
        target_path = context["relationships"].get(embed_id)
        if not target_path:
            return None
        archive: zipfile.ZipFile = context["archive"]
        try:
            payload = archive.read(target_path)
        except KeyError:
            return None
        filename = Path(target_path).name
        suffix = Path(filename).suffix or ".bin"
        asset_id = f"image-{len(context['assets']) + 1:03d}"
        asset_filename = f"{asset_id}{suffix}"
        asset_dir: Path | None = context.get("asset_dir")
        storage_path = ""
        public_url = ""
        document_id = context.get("document_id")
        if asset_dir is not None:
            target_file = asset_dir / asset_filename
            target_file.write_bytes(payload)
            storage_path = str(target_file)
            if document_id is not None:
                public_url = f"/api/knowledge/documents/{document_id}/assets/{asset_filename}"
        metadata = self._docx_image_metadata(element)
        asset = ExtractedAsset(
            asset_id=asset_id,
            filename=asset_filename,
            content_type=mimetypes.guess_type(asset_filename)[0] or "application/octet-stream",
            storage_path=storage_path,
            public_url=public_url,
            title=metadata.get("title"),
            description=metadata.get("description"),
        )
        context["asset_cache"][embed_id] = asset
        context["assets"].append(asset)
        return asset

    def _docx_image_metadata(self, element: ET.Element) -> dict[str, str | None]:
        for descendant in element.iter():
            if self._xml_local_name(descendant.tag) != "docPr":
                continue
            title = self._xml_attr(descendant, "title") or self._xml_attr(descendant, "name")
            description = self._xml_attr(descendant, "descr")
            return {"title": title, "description": description}
        return {"title": None, "description": None}

    def _normalize_docx_block_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = text.replace("\xa0", " ")
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n[ \t]+", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _omml_to_latex(self, element: ET.Element) -> str:
        local_name = self._xml_local_name(element.tag)
        if local_name == "oMathPara":
            formulas = [
                self._wrap_formula(self._omml_children_text(child), display=True)
                for child in element
                if self._xml_local_name(child.tag) == "oMath" and self._omml_children_text(child)
            ]
            if formulas:
                return "\n".join(formulas)
        if local_name == "oMath":
            formula = self._omml_children_text(element)
            return self._wrap_formula(formula, display=False)
        return self._omml_raw_text(element)

    def _omml_children_text(self, element: ET.Element) -> str:
        parts: list[str] = []
        if element.text and element.text.strip():
            parts.append(element.text)
        for child in element:
            if self._xml_local_name(child.tag).endswith("Pr"):
                continue
            part = self._omml_raw_text(child)
            if part:
                parts.append(part)
            if child.tail and child.tail.strip():
                parts.append(child.tail)
        return "".join(parts).strip()

    def _omml_raw_text(self, element: ET.Element) -> str:
        local_name = self._xml_local_name(element.tag)
        if local_name == "t":
            return (element.text or "").strip()
        if local_name in {"oMath", "oMathPara", "r", "e", "num", "den", "sup", "sub", "deg", "fName", "groupChr"}:
            return self._omml_children_text(element)
        if local_name == "f":
            numerator = self._omml_child_text(element, "num")
            denominator = self._omml_child_text(element, "den")
            if numerator or denominator:
                return fr"\frac{{{numerator}}}{{{denominator}}}"
            return ""
        if local_name == "sSup":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sup=self._omml_child_text(element, "sup"),
            )
        if local_name == "sSub":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sub=self._omml_child_text(element, "sub"),
            )
        if local_name == "sSubSup":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sub=self._omml_child_text(element, "sub"),
                sup=self._omml_child_text(element, "sup"),
            )
        if local_name == "rad":
            body = self._omml_child_text(element, "e")
            degree = self._omml_child_text(element, "deg")
            if degree:
                return fr"\sqrt[{degree}]{{{body}}}"
            return fr"\sqrt{{{body}}}"
        if local_name == "nary":
            operator = self._omml_nary_operator(element)
            sub = self._omml_child_text(element, "sub")
            sup = self._omml_child_text(element, "sup")
            body = self._omml_child_text(element, "e")
            result = operator
            if sub:
                result += f"_{{{sub}}}"
            if sup:
                result += f"^{{{sup}}}"
            if body:
                result += f" {body}"
            return result
        if local_name == "d":
            body = self._omml_child_text(element, "e")
            begin_char, end_char = self._omml_delimiters(element)
            return fr"\left{begin_char}{body}\right{end_char}"
        if local_name == "func":
            name = self._omml_child_text(element, "fName")
            body = self._omml_child_text(element, "e")
            return f"{name}{body}"
        if local_name == "limLow":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sub=self._omml_child_text(element, "lim"),
            )
        if local_name == "limUpp":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sup=self._omml_child_text(element, "lim"),
            )
        if local_name == "acc":
            body = self._omml_child_text(element, "e")
            accent = self._omml_accent_command(element)
            if accent:
                return fr"{accent}{{{body}}}"
            return body
        return self._omml_children_text(element)

    def _omml_child_text(self, element: ET.Element, child_name: str) -> str:
        child = self._omml_child(element, child_name)
        if child is None:
            return ""
        return self._omml_children_text(child)

    def _omml_child(self, element: ET.Element, child_name: str) -> ET.Element | None:
        for child in element:
            if self._xml_local_name(child.tag) == child_name:
                return child
        return None

    def _omml_nary_operator(self, element: ET.Element) -> str:
        operator = ""
        properties = self._omml_child(element, "naryPr")
        if properties is not None:
            character = self._omml_child(properties, "chr")
            if character is not None:
                operator = self._xml_attr(character, "val") or (character.text or "")
        return OMML_OPERATOR_MAP.get(operator, operator or r"\sum")

    def _omml_delimiters(self, element: ET.Element) -> tuple[str, str]:
        properties = self._omml_child(element, "dPr")
        if properties is None:
            return "(", ")"
        begin = self._omml_delimiter_value(properties, "begChr", default="(")
        end = self._omml_delimiter_value(properties, "endChr", default=")")
        return begin, end

    def _omml_delimiter_value(self, properties: ET.Element, child_name: str, *, default: str) -> str:
        child = self._omml_child(properties, child_name)
        if child is None:
            return default
        value = self._xml_attr(child, "val") or (child.text or "")
        if not value:
            return default
        return OMML_DELIMITER_MAP.get(value, value)

    def _omml_accent_command(self, element: ET.Element) -> str | None:
        properties = self._omml_child(element, "accPr")
        if properties is None:
            return None
        character = self._omml_child(properties, "chr")
        if character is None:
            return None
        value = self._xml_attr(character, "val") or (character.text or "")
        return OMML_ACCENT_MAP.get(value)

    def _latex_attach(self, *, base: str, sub: str | None = None, sup: str | None = None) -> str:
        result = f"{{{base}}}" if base else ""
        if sub:
            result += f"_{{{sub}}}"
        if sup:
            result += f"^{{{sup}}}"
        return result

    def _wrap_formula(self, formula: str, *, display: bool) -> str:
        if not formula:
            return ""
        delimiter = "$$" if display else "$"
        return f"{delimiter}{formula}{delimiter}"

    def _xml_local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _xml_namespace(self, tag: str) -> str | None:
        if tag.startswith("{") and "}" in tag:
            return tag[1:].split("}", 1)[0]
        return None

    def _xml_attr(self, element: ET.Element, attr_name: str) -> str | None:
        for key, value in element.attrib.items():
            if self._xml_local_name(key) == attr_name:
                return value
        return None

    def _extract_pdf_text(self, file_path: str) -> str:
        has_text_layer = self._pdf_has_text_layer(file_path)
        candidates = self._extract_pdf_candidates(file_path)
        if not candidates:
            if has_text_layer is False:
                raise RuntimeError("该 PDF 无可用文本层，疑似扫描版，无法有效提取文本。请上传可选中文字的 PDF，或将内容转为 Word/TXT 后重新上传。后续版本将支持 OCR。")
            raise RuntimeError("当前无法从 PDF 提取文本，请检查依赖或文件内容")

        best_pages = self._select_best_pdf_pages(candidates)
        text = self._normalize_pdf_text("\n".join(best_pages))
        if self._looks_like_scanned_pdf(text, len(best_pages)):
            raise RuntimeError("该 PDF 疑似扫描版，无法有效提取文本。请上传可选中文字的 PDF，或将内容转为 Word/TXT 后重新上传。后续版本将支持 OCR。")
        return text

    def _pdf_has_text_layer(self, file_path: str) -> bool | None:
        detectors = [
            self._pdf_has_text_layer_with_pymupdf,
            self._pdf_has_text_layer_with_pypdf,
        ]
        for detector in detectors:
            try:
                return detector(file_path)
            except Exception:
                continue
        return None

    def _extract_pdf_candidates(self, file_path: str) -> list[PDFExtractionCandidate]:
        extractors = [
            ("pymupdf", self._extract_pdf_pages_with_pymupdf),
            ("pdfplumber", self._extract_pdf_pages_with_pdfplumber),
            ("pypdf", self._extract_pdf_pages_with_pypdf),
        ]
        candidates: list[PDFExtractionCandidate] = []
        for name, extractor in extractors:
            try:
                pages = extractor(file_path)
            except Exception:
                continue
            if any(page.strip() for page in pages):
                candidates.append(PDFExtractionCandidate(extractor=name, pages=pages))
        return candidates

    def _select_best_pdf_pages(self, candidates: list[PDFExtractionCandidate]) -> list[str]:
        page_count = max(len(candidate.pages) for candidate in candidates)
        selected_pages: list[str] = []
        for index in range(page_count):
            page_options = []
            for candidate in candidates:
                if index < len(candidate.pages):
                    page_text = candidate.pages[index]
                    if page_text.strip():
                        page_options.append((self._score_extracted_text(page_text), page_text))
            if page_options:
                best_page = max(page_options, key=lambda item: item[0])[1]
                selected_pages.append(best_page)
        return selected_pages

    def _extract_pdf_pages_with_pymupdf(self, file_path: str) -> list[str]:
        import fitz

        with fitz.open(file_path) as document:
            return [page.get_text("text") or "" for page in document]

    def _extract_pdf_pages_with_pdfplumber(self, file_path: str) -> list[str]:
        import pdfplumber

        with pdfplumber.open(file_path) as document:
            return [page.extract_text() or "" for page in document.pages]

    def _extract_pdf_pages_with_pypdf(self, file_path: str) -> list[str]:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        return [page.extract_text() or "" for page in reader.pages]

    def _pdf_has_text_layer_with_pymupdf(self, file_path: str) -> bool:
        import fitz

        with fitz.open(file_path) as document:
            if len(document) == 0:
                return False
            return any((page.get_text("text") or "").strip() for page in document)

    def _pdf_has_text_layer_with_pypdf(self, file_path: str) -> bool:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        if not reader.pages:
            return False
        return any((page.extract_text() or "").strip() for page in reader.pages)

    def _score_extracted_text(self, text: str) -> float:
        normalized = self._normalize_pdf_text(text)
        total = max(len(normalized), 1)
        chinese = len(re.findall(r"[\u4e00-\u9fff]", normalized))
        alnum = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", normalized))
        whitespace = len(re.findall(r"\s", normalized))
        replacement = normalized.count("�")
        private = len(re.findall(r"[\ue000-\uf8ff]", normalized))
        control = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", normalized))
        noisy_symbols = len(re.findall(r"[~`|\\]+", normalized))

        chinese_ratio = chinese / total
        alnum_ratio = alnum / total
        whitespace_ratio = whitespace / total
        density_bonus = min(total / 4000, 2.0)
        whitespace_penalty = abs(whitespace_ratio - 0.12) * 1.5
        bad_char_penalty = replacement * 0.8 + private * 0.5 + control * 0.5 + noisy_symbols * 0.2

        return (chinese_ratio * 4.0) + (alnum_ratio * 3.0) + density_bonus - whitespace_penalty - (bad_char_penalty / max(total / 100, 1))

    def _normalize_pdf_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        lines = []
        for raw_line in text.split("\n"):
            line = re.sub(r"[ \t\u3000]+", " ", raw_line).strip()
            if self._should_drop_noisy_pdf_line(line):
                continue
            lines.append(line)

        normalized_lines: list[str] = []
        previous_blank = False
        for line in lines:
            if not line:
                if not previous_blank:
                    normalized_lines.append("")
                previous_blank = True
                continue
            normalized_lines.append(line)
            previous_blank = False

        return "\n".join(normalized_lines).strip()

    def _should_drop_noisy_pdf_line(self, line: str) -> bool:
        if not line:
            return False
        if len(line) <= 10 and "~" in line:
            return True
        if len(line) <= 8 and re.search(r"[A-Za-z]{2,}.*[^\w\s\u4e00-\u9fff]", line):
            return True
        return False

    def _looks_like_scanned_pdf(self, text: str, page_count: int) -> bool:
        meaningful_chars = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text))
        if page_count <= 1:
            return meaningful_chars < 20
        return (meaningful_chars / max(page_count, 1)) < 50


rag_service = RagService()
