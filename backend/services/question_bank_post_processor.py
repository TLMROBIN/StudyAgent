from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1
import re
from typing import Any

from backend.models.knowledge import KnowledgeDocument

REQUIRED_IMAGE_HINTS = (
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
OPTIONAL_IMAGE_HINTS = (
    "图像",
    "示意图",
    "函数图像",
    "坐标图",
    "统计图",
)
INLINE_QUESTION_MARKER_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:第\s*\d+\s*题|\d{1,3}\s*[.．、:：)]|[（(]\d{1,3}[)）])\s*"
)
ANSWER_PAIRING_SUSPECT_PATTERN = re.compile(r"(?:^|\n)\s*\d{1,3}\s*[.．、:：)]\s*答案")


@dataclass(slots=True)
class QuestionBankChunkCandidate:
    question_number: str
    question_text: str
    answer_text: str | None = None
    explanation_text: str | None = None
    asset_refs: list[dict[str, Any]] = field(default_factory=list)
    source_format: str | None = None
    source_locator: str | None = None
    parser_backend: str | None = None
    parser_provenance: dict[str, Any] | None = None
    page_start: int | None = None
    page_end: int | None = None
    source_pages: list[int] = field(default_factory=list)
    source_block_types: list[str] = field(default_factory=list)
    structure_path: list[str] = field(default_factory=list)


class QuestionBankPostProcessor:
    def build_metadata(
        self,
        document: KnowledgeDocument,
        candidate: QuestionBankChunkCandidate,
    ) -> dict[str, Any]:
        question_number = str(candidate.question_number or "").strip()
        question_text = str(candidate.question_text or "").strip()
        answer_text = self._clean_optional_text(candidate.answer_text)
        explanation_text = self._clean_optional_text(candidate.explanation_text)
        asset_refs = self._dedupe_asset_refs(candidate.asset_refs)
        structure_path = [str(item).strip() for item in candidate.structure_path if str(item or "").strip()]
        source_pages = sorted({int(page) for page in candidate.source_pages if page})
        source_block_types = sorted({str(item).strip() for item in candidate.source_block_types if str(item or "").strip()})
        source_format = str(candidate.source_format or self._source_format(document)).strip() or "unknown"
        source_locator = self._source_locator(candidate, question_number, question_text, source_pages)
        image_expectation = self._image_expectation(question_text)
        image_binding_status = self._image_binding_status(image_expectation, asset_refs)
        quality_flags = self._quality_flags(
            question_text=question_text,
            answer_text=answer_text,
            explanation_text=explanation_text,
            image_binding_status=image_binding_status,
        )
        question_uid = f"qb:{document.id}:{source_locator or question_number or 'unknown'}"

        return {
            "chunk_kind": "question_item",
            "question_number": question_number or None,
            "question_text": question_text or None,
            "answer_text": answer_text,
            "explanation_text": explanation_text,
            "contains_images": bool(asset_refs),
            "asset_refs": asset_refs,
            "image_count": len(asset_refs),
            "structure_path": structure_path,
            "source_format": source_format,
            "source_locator": source_locator,
            "parser_backend": self._clean_optional_text(candidate.parser_backend),
            "parser_provenance": candidate.parser_provenance or None,
            "page_start": candidate.page_start,
            "page_end": candidate.page_end,
            "source_pages": source_pages or None,
            "source_block_types": source_block_types or None,
            "image_expectation": image_expectation,
            "image_binding_status": image_binding_status,
            "quality_flags": quality_flags,
            "question_uid": question_uid,
        }

    def _clean_optional_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def _source_format(self, document: KnowledgeDocument) -> str:
        mime_type = str(document.mime_type or "").lower()
        if "pdf" in mime_type:
            return "pdf"
        if "wordprocessingml.document" in mime_type:
            return "docx"
        if "text/" in mime_type:
            return "txt"
        return "unknown"

    def _source_locator(
        self,
        candidate: QuestionBankChunkCandidate,
        question_number: str,
        question_text: str,
        source_pages: list[int],
    ) -> str:
        if candidate.source_locator:
            return str(candidate.source_locator).strip()
        parts: list[str] = []
        if question_number:
            parts.append(f"question:{question_number}")
        if candidate.page_start and candidate.page_end:
            if candidate.page_start == candidate.page_end:
                parts.append(f"page:{candidate.page_start}")
            else:
                parts.append(f"pages:{candidate.page_start}-{candidate.page_end}")
        elif source_pages:
            if len(source_pages) == 1:
                parts.append(f"page:{source_pages[0]}")
            else:
                parts.append(f"pages:{source_pages[0]}-{source_pages[-1]}")
        if parts:
            return "|".join(parts)
        text_fingerprint = sha1(question_text.encode("utf-8")).hexdigest()[:12] if question_text else "empty"
        return f"question:{question_number or text_fingerprint}"

    def _image_expectation(self, question_text: str) -> str:
        if any(token in question_text for token in REQUIRED_IMAGE_HINTS):
            return "required"
        if any(token in question_text for token in OPTIONAL_IMAGE_HINTS):
            return "optional"
        return "not_needed"

    def _image_binding_status(self, image_expectation: str, asset_refs: list[dict[str, Any]]) -> str:
        if asset_refs:
            return "bound"
        if image_expectation == "required":
            return "missing_required"
        if image_expectation == "optional":
            return "optional_unbound"
        return "none_needed"

    def _quality_flags(
        self,
        *,
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
        if self._looks_like_multi_question(question_text):
            flags.append("multi_question_suspected")
        if self._looks_like_answer_pairing_issue(answer_text, explanation_text):
            flags.append("answer_pairing_suspected")
        return flags

    def _looks_like_multi_question(self, question_text: str) -> bool:
        stripped = question_text.strip()
        if not stripped:
            return False
        matches = list(INLINE_QUESTION_MARKER_PATTERN.finditer(stripped))
        return len(matches) > 1 or bool(INLINE_QUESTION_MARKER_PATTERN.search(f"\n{stripped[1:]}"))

    def _looks_like_answer_pairing_issue(self, answer_text: str | None, explanation_text: str | None) -> bool:
        answer = str(answer_text or "").strip()
        explanation = str(explanation_text or "").strip()
        return bool(ANSWER_PAIRING_SUSPECT_PATTERN.search(answer) or ANSWER_PAIRING_SUSPECT_PATTERN.search(explanation))

    def _dedupe_asset_refs(self, asset_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for asset in asset_refs:
            if not isinstance(asset, dict):
                continue
            key = (
                str(asset.get("asset_id") or ""),
                str(asset.get("filename") or ""),
                str(asset.get("url") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(asset)
        return deduped
