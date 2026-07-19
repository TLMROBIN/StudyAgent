from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import mimetypes
import re
import shutil
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from backend.config import Settings, get_settings
from backend.grade_utils import HIGH_SCHOOL_GRADE_LABELS, extract_grade_levels
from backend.models.knowledge import DifficultyLevel, KnowledgeChunk, KnowledgeDocument, ResourceType
# Chunk-builder mixins and their constants. The constants are re-exported here (noqa: F401)
# so that every symbol historically importable from backend.services.rag_service keeps working.
from backend.services.chunking.docx_chunk_builder import (  # noqa: F401
    DOCX_MATH_NS,
    DOCX_OLE_NS,
    DOCX_PACKAGE_REL_NS,
    DOCX_REL_NS,
    DOCX_VML_NS,
    DOCX_WORD_NS,
    LEGACY_DOCX_OLE_PROG_IDS,
    LEGACY_QUESTION_DOCX_FORMULA_MESSAGE,
    OLE_CF_MAGIC,
    OLE_END_OF_CHAIN,
    OLE_FREE_SECTOR,
    OLE_METADATA_STRINGS,
    OLE_UINT32_SIZE,
    OMML_ACCENT_MAP,
    OMML_DELIMITER_MAP,
    OMML_OPERATOR_MAP,
    DocxChunkBuilderMixin,
)
from backend.services.chunking.models import ExtractionResult, PreparedChunk  # noqa: F401
from backend.services.chunking.pdf_chunk_builder import (  # noqa: F401
    PdfChunkBuilderMixin,
    PDFExtractionCandidate,
)
from backend.services.chunking.question_chunk_builder import (  # noqa: F401
    ANSWER_LINE_PATTERN,
    ANSWER_ONLY_TOKEN_PATTERN,
    DIFFICULTY_LINE_PATTERN,
    EXPLANATION_LINE_PATTERN,
    INLINE_EXPLANATION_SEGMENT_PATTERN,
    KNOWLEDGE_POINTS_LINE_PATTERN,
    QUESTION_CATEGORY_HEADING_PATTERN,
    QUESTION_SECTION_HEADING_PATTERN,
    QUESTION_START_PATTERN,
    QuestionChunkBuilderMixin,
    QuestionChunkDraft,
)
from backend.services.chunking.text_chunk_builder import (  # noqa: F401
    CHUNK_BOUNDARY_HINTS,
    INLINE_MATH_TERMINAL_PATTERN,
    MARKDOWN_BLOCK_START_PATTERN,
    PLAIN_TEXT_SPLIT_PATTERN,
    PUNCTUATION_ONLY_PATTERN,
    TextChunkBuilderMixin,
)
from backend.services.embed_service import EmbedService, embed_service
from backend.services.mineru_remote_service import mineru_remote_service
from backend.services.mineru_service import mineru_service
from backend.services.pdf_parse_bridge import PDFParseBridge
from backend.services.pdf_parse_types import ExtractedAsset, PDFParseResult
from backend.services.question_bank_post_processor import (  # noqa: F401
    QuestionBankChunkCandidate,
    QuestionBankPostProcessor,
)
from backend.services.vector_store_service import VectorStoreService, vector_store_service

logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}
# 分学科检索策略。缺省值 = 现行为（零加权、全局 top_k、不过滤低分行）。
# 可用 env RAG_SUBJECT_POLICIES（JSON 字符串）逐科深覆盖，非法 JSON 时告警并回退本表。
DEFAULT_SUBJECT_RETRIEVAL_POLICY: dict[str, object] = {
    "top_k": None,               # None → settings.rag_top_k
    "question_bank_bonus": 0.0,  # chunk_kind == question_item 的加权
    "source_material_bonus": 0.0,  # 讲义/教材/拓展类资源加权
    "min_base_score": None,      # 向量检索基础分下限（None = 不过滤）
}
SUBJECT_RETRIEVAL_POLICIES: dict[str, dict[str, object]] = {
    "数学": {"question_bank_bonus": 0.12},
    "物理": {"question_bank_bonus": 0.12},
    "语文": {"source_material_bonus": 0.10, "top_k": 5},
    "历史": {"source_material_bonus": 0.10, "top_k": 5},
    "政治": {"source_material_bonus": 0.10, "top_k": 5},
    # 英语/化学/生物/地理：全默认（现行为）
}
SOURCE_MATERIAL_RESOURCE_TYPES = {
    ResourceType.KNOWLEDGE_NOTE.value,
    ResourceType.TEXTBOOK.value,
    ResourceType.EXTENSION.value,
}
CHAPTER_AWARE_RESOURCE_TYPES = {
    ResourceType.KNOWLEDGE_NOTE.value,
    ResourceType.TEXTBOOK.value,
    ResourceType.EXERCISE.value,
    ResourceType.QUESTION_SET.value,
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
ASSET_MARKER_PATTERN = re.compile(r"\[\[asset:([A-Za-z0-9_.-]+)\]\]")
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


class UnsupportedQuestionDocxError(RuntimeError):
    """Raised when a question-resource DOCX contains unsupported legacy formulas."""


@dataclass
class QuestionProfile:
    question_type: str
    preferred_resources: list[str]
    desired_difficulty: str | None = None
    prefer_extension: bool = False


@dataclass
class QuestionRecommendationCandidate:
    score: float
    row: KnowledgeChunk
    semantic_score: float
    metadata_matches: int
    tag_matches: int
    chapter_match: bool
    section_match: bool


class RagService(DocxChunkBuilderMixin, PdfChunkBuilderMixin, QuestionChunkBuilderMixin, TextChunkBuilderMixin):
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

    def retrieve(self, db: Session, subject: str, question: str, *, student_grade: int | None = None) -> RetrievalResult:
        profile = self._infer_question_profile(question)
        effective_top_k = self._effective_top_k(subject)
        try:
            matches = self.vector_store.query(subject, question, effective_top_k * 2)
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
                    subject=subject,
                )
                ordered_rows = self._exclude_disabled_question_rows(ordered_rows)
                if ordered_rows:
                    return RetrievalResult(context=self.format_context(ordered_rows), chunks=ordered_rows)
        except Exception:
            logger.exception("Vector retrieval failed for subject=%s; using database fallback", subject)

        return self._fallback_retrieve(db, subject, question, profile=profile, student_grade=student_grade)

    def recommend_questions(
        self,
        db: Session,
        subject: str,
        question: str,
        *,
        student_grade: int | None = None,
        limit: int = 3,
        difficulty_preference: str = DifficultyLevel.BASIC.value,
    ) -> list[KnowledgeChunk]:
        rows = db.scalars(
            select(KnowledgeChunk)
            .options(selectinload(KnowledgeChunk.document))
            .where(KnowledgeChunk.subject == subject)
        ).all()
        rows = self._exclude_disabled_question_rows(rows)
        preferred_rows = [row for row in rows if self._question_row_tier(row) == "preferred"]
        if not preferred_rows and not any(self._question_row_tier(row) == "fallback" for row in rows):
            return []

        query_embedding = self.embedder.embed_text(question)
        profile = self._recommendation_profile(question)
        selected_candidates: list[QuestionRecommendationCandidate] = []
        seen_keys: set[tuple[int, str]] = set()
        for candidate in self._filter_recommendation_candidates(
            self._score_question_rows(preferred_rows, question, student_grade, query_embedding, profile),
            tier="preferred",
        ):
            key = self._question_row_key(candidate.row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected_candidates.append(candidate)

        preferred_document_ids = {row.document_id for row in preferred_rows}
        fallback_rows = [
            row
            for row in rows
            if self._question_row_tier(row) == "fallback" and row.document_id not in preferred_document_ids
        ]
        for candidate in self._filter_recommendation_candidates(
            self._score_question_rows(fallback_rows, question, student_grade, query_embedding, profile),
            tier="fallback",
        ):
            key = self._question_row_key(candidate.row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected_candidates.append(candidate)
        return self._select_recommendation_rows_by_difficulty(
            selected_candidates,
            limit=limit,
            difficulty_preference=difficulty_preference,
        )

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
        contents = [row.content for row in rows]
        content_embeddings = self.embedder.embed_texts(contents)
        scored_rows = []
        for row, content_embedding in zip(rows, content_embeddings, strict=False):
            score = self.embedder.cosine_similarity(query_embedding, content_embedding)
            keyword_bonus = sum(1 for char in question[:12] if char and char in row.content) / 20.0
            scored_rows.append((score + keyword_bonus, row))
        best = self._rerank_rows(
            question=question,
            profile=profile or self._infer_question_profile(question),
            scored_rows=scored_rows,
            student_grade=student_grade,
            subject=subject,
        )
        best = self._exclude_disabled_question_rows(best)
        return RetrievalResult(context=self.format_context(best), chunks=best)

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
            if self.settings.pdf_parser_backend == "mineru_remote":
                if document_id is None or task_id is None:
                    raise RuntimeError("MinerU remote PDF parsing requires document_id and task_id")
                parsed_pdf = mineru_remote_service.parse_pdf(file_path, task_id=task_id, document_id=document_id)
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
                docx_parser = mineru_remote_service if self.settings.pdf_parser_backend == "mineru_remote" else mineru_service
                parsed_docx = docx_parser.parse_docx(file_path, task_id=task_id, document_id=document_id)
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

    def health_snapshot(self) -> dict[str, dict | str | bool]:
        pdf_parser = (
            mineru_remote_service.health_snapshot()
            if self.settings.pdf_parser_backend == "mineru_remote"
            else mineru_service.health_snapshot()
        )
        return {
            "embedding": self.embedder.health_snapshot(),
            "vector_store": self.vector_store.health_snapshot(),
            "pdf_parser": pdf_parser,
        }

    def document_asset_dir(self, document_id: int) -> Path:
        return Path(self.settings.task_artifact_path) / "knowledge" / str(document_id)

    def clear_document_artifacts(self, document_id: int) -> None:
        shutil.rmtree(self.document_asset_dir(document_id), ignore_errors=True)

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

    # ---- 分学科检索策略 ----

    _env_subject_policies_cache: tuple[str, dict] | None = None

    def _env_subject_policies(self) -> dict:
        raw = (getattr(self.settings, "rag_subject_policies_json", "") or "").strip()
        if not raw:
            return {}
        cache = self._env_subject_policies_cache
        if cache is not None and cache[0] == raw:
            return cache[1]
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("RAG_SUBJECT_POLICIES must be a JSON object")
        except ValueError:
            logger.warning("RAG_SUBJECT_POLICIES 非法 JSON，忽略并回退内置分学科检索策略表")
            parsed = {}
        self._env_subject_policies_cache = (raw, parsed)
        return parsed

    def _subject_policy(self, subject: str | None) -> dict[str, object]:
        policy = dict(DEFAULT_SUBJECT_RETRIEVAL_POLICY)
        if not subject:
            return policy
        builtin = SUBJECT_RETRIEVAL_POLICIES.get(subject)
        if isinstance(builtin, dict):
            policy.update(builtin)
        override = self._env_subject_policies().get(subject)
        if isinstance(override, dict):
            policy.update({key: value for key, value in override.items() if key in DEFAULT_SUBJECT_RETRIEVAL_POLICY})
        return policy

    def _effective_top_k(self, subject: str | None) -> int:
        top_k = self._subject_policy(subject).get("top_k")
        if isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0:
            return top_k
        return self.settings.rag_top_k

    def _rerank_rows(
        self,
        *,
        question: str,
        profile: QuestionProfile,
        scored_rows: list[tuple[float, KnowledgeChunk]],
        student_grade: int | None,
        subject: str | None = None,
    ) -> list[KnowledgeChunk]:
        policy = self._subject_policy(subject)
        min_base_score = policy.get("min_base_score")
        if isinstance(min_base_score, (int, float)) and not isinstance(min_base_score, bool):
            filtered = [(score, row) for score, row in scored_rows if score >= float(min_base_score)]
            if filtered:  # 过滤后为空则回退全量（不确定时保留）
                scored_rows = filtered
        rescored: list[tuple[float, KnowledgeChunk]] = []
        for base_score, row in scored_rows:
            final_score = base_score + self._metadata_score(
                row,
                question,
                profile,
                student_grade,
                recommendation_mode=False,
                subject=subject,
            )
            rescored.append((final_score, row))
        rescored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in rescored[: self._effective_top_k(subject)]]

    def _metadata_score(
        self,
        row: KnowledgeChunk,
        question: str,
        profile: QuestionProfile,
        student_grade: int | None,
        *,
        recommendation_mode: bool,
        subject: str | None = None,
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
        tag_candidates = list(document.tags)
        metadata_tags = metadata.get("tags")
        if isinstance(metadata_tags, list):
            tag_candidates.extend(
                str(tag).strip() for tag in metadata_tags if str(tag).strip()
            )
        for tag in list(dict.fromkeys(tag_candidates))[:8]:
            if tag.lower() in question_lowered:
                score += 0.08
        if subject:
            policy = self._subject_policy(subject)
            question_bank_bonus = policy.get("question_bank_bonus")
            if (
                isinstance(question_bank_bonus, (int, float))
                and question_bank_bonus
                and metadata.get("chunk_kind") == "question_item"
            ):
                score += float(question_bank_bonus)
            source_material_bonus = policy.get("source_material_bonus")
            if (
                isinstance(source_material_bonus, (int, float))
                and source_material_bonus
                and resource_type in SOURCE_MATERIAL_RESOURCE_TYPES
            ):
                score += float(source_material_bonus)
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

    def _question_row_is_disabled(self, row: KnowledgeChunk) -> bool:
        return bool(getattr(row, "is_disabled", False))

    def _exclude_disabled_question_rows(
        self,
        rows: list[KnowledgeChunk],
    ) -> list[KnowledgeChunk]:
        return [row for row in rows if not self._question_row_is_disabled(row)]

    def _question_row_tier(self, row: KnowledgeChunk) -> str | None:
        if self._question_row_is_disabled(row):
            return None
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
    ) -> list[QuestionRecommendationCandidate]:
        if not rows:
            return []
        candidate_texts = [self._question_candidate_text(row) for row in rows]
        candidate_embeddings = self.embedder.embed_texts(candidate_texts)
        scored_rows: list[QuestionRecommendationCandidate] = []
        for row, candidate_text, candidate_embedding in zip(rows, candidate_texts, candidate_embeddings):
            semantic_score = self.embedder.cosine_similarity(query_embedding, candidate_embedding)
            semantic_score += sum(1 for char in question[:16] if char and char in candidate_text) / 24.0
            metadata_matches, tag_matches, chapter_match, section_match = self._recommendation_metadata_matches(row, question)
            score = semantic_score
            score += self._metadata_score(row, question, profile, student_grade, recommendation_mode=True)
            score += self._question_recommendation_bonus(row, question)
            scored_rows.append(
                QuestionRecommendationCandidate(
                    score=score,
                    row=row,
                    semantic_score=semantic_score,
                    metadata_matches=metadata_matches,
                    tag_matches=tag_matches,
                    chapter_match=chapter_match,
                    section_match=section_match,
                )
            )
        scored_rows.sort(key=lambda item: item.score, reverse=True)
        return scored_rows

    def _recommendation_metadata_matches(
        self,
        row: KnowledgeChunk,
        question: str,
    ) -> tuple[int, int, bool, bool]:
        metadata = row.metadata_json or {}
        retrieval_metadata = self._retrieval_structure(metadata, row.document)
        question_lowered = question.lower()
        question_key = self._structure_key(question_lowered) or ""

        chapter = str(retrieval_metadata.get("chapter") or "").strip()
        section = str(retrieval_metadata.get("section") or "").strip()
        chapter_key = str(retrieval_metadata.get("chapter_key") or "").strip()
        section_key = str(retrieval_metadata.get("section_key") or "").strip()

        chapter_match = bool(
            (chapter and chapter.lower() in question_lowered)
            or (chapter_key and chapter_key in question_key)
        )
        section_match = bool(
            (section and section.lower() in question_lowered)
            or (section_key and section_key in question_key)
        )

        tag_candidates: list[str] = []
        document = row.document
        if document:
            tag_candidates.extend(document.tags)
        metadata_tags = metadata.get("tags")
        if isinstance(metadata_tags, list):
            tag_candidates.extend(
                str(tag).strip() for tag in metadata_tags if str(tag).strip()
            )
        tag_matches = sum(
            1 for tag in dict.fromkeys(tag_candidates) if tag and tag.lower() in question_lowered
        )
        metadata_matches = tag_matches + int(chapter_match) + int(section_match)
        return metadata_matches, tag_matches, chapter_match, section_match

    def _filter_recommendation_candidates(
        self,
        candidates: list[QuestionRecommendationCandidate],
        *,
        tier: str,
    ) -> list[QuestionRecommendationCandidate]:
        if not candidates:
            return []
        top_score = candidates[0].score
        accepted: list[QuestionRecommendationCandidate] = []
        for candidate in candidates:
            if candidate.score < self._recommendation_min_score(tier=tier):
                continue
            if candidate.score < top_score - self._recommendation_relative_window(tier=tier):
                continue
            if candidate.metadata_matches > 0:
                accepted.append(candidate)
                continue
            if candidate.semantic_score >= self._recommendation_semantic_rescue_floor(tier=tier):
                accepted.append(candidate)
        return accepted

    def _recommendation_min_score(self, *, tier: str) -> float:
        return 1.15 if tier == "preferred" else 1.28

    def _recommendation_relative_window(self, *, tier: str) -> float:
        return 0.22 if tier == "preferred" else 0.16

    def _recommendation_semantic_rescue_floor(self, *, tier: str) -> float:
        return 0.18 if tier == "preferred" else 0.28

    def _select_recommendation_rows_by_difficulty(
        self,
        candidates: list[QuestionRecommendationCandidate],
        *,
        limit: int,
        difficulty_preference: str,
    ) -> list[KnowledgeChunk]:
        if not candidates:
            return []
        minimum_rank = {
            DifficultyLevel.BASIC.value: 0,
            DifficultyLevel.STANDARD.value: 1,
            DifficultyLevel.ADVANCED.value: 2,
        }.get(difficulty_preference, 0)
        filtered_candidates = [
            candidate
            for candidate in candidates
            if self._difficulty_rank_for_row(candidate.row) >= minimum_rank
        ]
        if not filtered_candidates:
            return []
        filtered_candidates.sort(
            key=lambda candidate: (
                self._difficulty_rank_for_row(candidate.row),
                -candidate.score,
            )
        )
        return [candidate.row for candidate in filtered_candidates[:limit]]

    def _difficulty_rank_for_row(self, row: KnowledgeChunk) -> int:
        difficulty = self._question_row_difficulty(row)
        mapping = {
            DifficultyLevel.BASIC.value: 0,
            DifficultyLevel.STANDARD.value: 1,
            DifficultyLevel.ADVANCED.value: 2,
            DifficultyLevel.CHALLENGE.value: 3,
        }
        return mapping.get(difficulty or DifficultyLevel.STANDARD.value, 1)

    def _question_row_difficulty(self, row: KnowledgeChunk) -> str | None:
        metadata = row.metadata_json or {}
        document = row.document
        value = metadata.get("difficulty")
        if value:
            return str(value)
        if document and document.difficulty:
            return str(document.difficulty)
        return None

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

    def format_context(self, rows: list[KnowledgeChunk]) -> str:
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

rag_service = RagService()
