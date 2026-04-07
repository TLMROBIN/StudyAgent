from __future__ import annotations

from hashlib import sha1
import re
from typing import Any

from backend.models.knowledge import KnowledgeDocument

QUESTION_BANK_METADATA_PRESERVE_KEYS = {
    "chunk_kind",
    "question_number",
    "question_text",
    "answer_text",
    "explanation_text",
    "contains_images",
    "asset_refs",
    "image_count",
    "parser_backend",
    "parser_provenance",
    "page_start",
    "page_end",
    "source_pages",
    "source_block_types",
    "structure_path",
    "source_format",
    "source_locator",
    "image_expectation",
    "image_binding_status",
    "quality_flags",
    "question_uid",
}

QUESTION_IMAGE_REQUIRED_HINTS = (
    "如图",
    "下图",
    "图示",
    "图中",
    "根据图像",
    "看图",
    "电路图",
    "受力图",
    "几何图形",
    "装置图",
)
QUESTION_IMAGE_OPTIONAL_HINTS = ("示意图", "图表", "表格", "装置", "模型", "实验图")
QUESTION_START_PATTERN = re.compile(
    r"^\s*(?:第\s*\d+\s*题|\d{1,3}\s*[.．、:：)]|[（(]\d{1,3}[)）])\s*",
    re.MULTILINE,
)


class QuestionBankPostProcessor:
    @staticmethod
    def compose_question_chunk_text(
        *,
        number: str,
        question_text: str,
        answer_text: str | None,
        explanation_text: str | None,
    ) -> str:
        parts = [f"第{number}题", f"题目：\n{str(question_text or '').strip()}"]
        if answer_text:
            parts.append(f"答案：\n{str(answer_text).strip()}")
        if explanation_text:
            parts.append(f"解析：\n{str(explanation_text).strip()}")
        return "\n\n".join(part for part in parts if part.strip()).strip()

    @classmethod
    def build_question_metadata(
        cls,
        document: KnowledgeDocument,
        *,
        question_number: str,
        question_text: str,
        answer_text: str | None,
        explanation_text: str | None,
        asset_refs: list[dict[str, Any]] | None,
        chapter: str | None = None,
        section: str | None = None,
        structure_path: list[str] | None = None,
        source_format: str | None = None,
        source_locator: str | None = None,
        parser_backend: str | None = None,
        parser_provenance: dict[str, Any] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_question = str(question_text or "").strip()
        normalized_answer = str(answer_text or "").strip() or None
        normalized_explanation = str(explanation_text or "").strip() or None
        normalized_assets = [dict(item) for item in (asset_refs or [])]
        normalized_structure = cls._normalize_structure_path(structure_path, chapter, section)
        resolved_source_format = cls._resolve_source_format(source_format, document.filename)
        resolved_source_locator = str(source_locator or "").strip() or cls._default_source_locator(question_number)
        image_expectation = cls._image_expectation(normalized_question)
        image_binding_status = cls._image_binding_status(image_expectation, normalized_assets)
        quality_flags = cls._quality_flags(
            question_number=question_number,
            question_text=normalized_question,
            answer_text=normalized_answer,
            explanation_text=normalized_explanation,
            image_binding_status=image_binding_status,
        )
        metadata: dict[str, Any] = {
            "chunk_kind": "question_item",
            "question_number": question_number,
            "question_text": normalized_question,
            "answer_text": normalized_answer,
            "explanation_text": normalized_explanation,
            "contains_images": bool(normalized_assets),
            "asset_refs": normalized_assets,
            "image_count": len(normalized_assets),
            "structure_path": normalized_structure,
            "source_format": resolved_source_format,
            "source_locator": resolved_source_locator,
            "parser_backend": parser_backend,
            "parser_provenance": parser_provenance,
            "image_expectation": image_expectation,
            "image_binding_status": image_binding_status,
            "quality_flags": quality_flags,
            "question_uid": cls._question_uid(document.id, resolved_source_locator, question_number, normalized_question),
        }
        if extra_metadata:
            metadata.update({key: value for key, value in extra_metadata.items() if value is not None})
        return metadata

    @staticmethod
    def _normalize_structure_path(
        structure_path: list[str] | None,
        chapter: str | None,
        section: str | None,
    ) -> list[str]:
        raw_items = structure_path if structure_path is not None else [chapter, section]
        return [str(item).strip() for item in raw_items if str(item or "").strip()]

    @staticmethod
    def _resolve_source_format(source_format: str | None, filename: str | None) -> str:
        normalized = str(source_format or "").strip().lower()
        if normalized:
            return normalized
        suffix = str(filename or "").rsplit(".", 1)
        if len(suffix) == 2 and suffix[-1]:
            return suffix[-1].lower()
        return "document"

    @staticmethod
    def _default_source_locator(question_number: str) -> str:
        normalized = str(question_number or "").strip()
        return f"question:{normalized}" if normalized else "question:unknown"

    @classmethod
    def _image_expectation(cls, question_text: str) -> str:
        normalized = str(question_text or "").strip()
        if any(token in normalized for token in QUESTION_IMAGE_REQUIRED_HINTS):
            return "required"
        if any(token in normalized for token in QUESTION_IMAGE_OPTIONAL_HINTS):
            return "optional"
        return "not_needed"

    @staticmethod
    def _image_binding_status(image_expectation: str, asset_refs: list[dict[str, Any]]) -> str:
        has_assets = bool(asset_refs)
        if image_expectation == "required":
            return "bound" if has_assets else "missing_required"
        if image_expectation == "optional":
            return "bound" if has_assets else "optional_unbound"
        return "none_needed"

    @classmethod
    def _quality_flags(
        cls,
        *,
        question_number: str,
        question_text: str,
        answer_text: str | None,
        explanation_text: str | None,
        image_binding_status: str,
    ) -> list[str]:
        flags: list[str] = []
        if not question_text:
            flags.append("empty_question_text")
        if image_binding_status == "missing_required":
            flags.append("missing_required_image")
        if explanation_text and not answer_text:
            flags.append("answer_pairing_suspected")
        if cls._contains_multiple_questions(question_number, question_text):
            flags.append("multi_question_suspected")
        return flags

    @staticmethod
    def _contains_multiple_questions(question_number: str, question_text: str) -> bool:
        matches = [match.group(0) for match in QUESTION_START_PATTERN.finditer(str(question_text or ""))]
        if not matches:
            return False
        normalized_number = str(question_number or "").strip()
        if len(matches) >= 2:
            return True
        if normalized_number and not matches[0].strip().startswith(f"第{normalized_number}题"):
            first_line = str(question_text or "").splitlines()[0].strip() if question_text else ""
            return bool(first_line) and first_line.startswith(tuple("0123456789（("))
        return False

    @staticmethod
    def _question_uid(
        document_id: int | None,
        source_locator: str,
        question_number: str,
        question_text: str,
    ) -> str:
        normalized_document_id = document_id if document_id is not None else "unknown"
        normalized_locator = str(source_locator or "").strip()
        if not normalized_locator:
            normalized_locator = f"question:{str(question_number or '').strip() or 'unknown'}"
        digest = sha1(question_text.encode("utf-8")).hexdigest()[:10] if question_text else "empty"
        return f"{normalized_document_id}:{normalized_locator}:{digest}"
