from __future__ import annotations

from collections import Counter
from dataclasses import replace
import re
from typing import TYPE_CHECKING, Any

from backend.models.knowledge import KnowledgeDocument, ResourceType
from backend.services.pdf_parse_types import ExtractedAsset, PDFParseResult

if TYPE_CHECKING:
    from backend.services.rag_service import PreparedChunk, RagService


class PDFParseBridge:
    def __init__(self, rag_service: "RagService") -> None:
        self.rag_service = rag_service

    def prepare_chunks(self, document: KnowledgeDocument, parsed_pdf: PDFParseResult) -> list["PreparedChunk"]:
        parsed_pdf = self._clean_pdf_parse_result(document, parsed_pdf)
        asset_map = {asset.asset_id: asset for asset in parsed_pdf.assets}
        if (document.resource_type or ResourceType.KNOWLEDGE_NOTE.value) in self.rag_service.QUESTION_RESOURCE_TYPES:
            prepared_questions = self._prepare_question_chunks_from_blocks(document, parsed_pdf, asset_map)
            if not prepared_questions:
                prepared_questions = self.rag_service._prepare_question_chunks(document, parsed_pdf.text, asset_map)
            if prepared_questions:
                for chunk in prepared_questions:
                    chunk.metadata.setdefault("parser_backend", parsed_pdf.parser_backend)
                    chunk.metadata.setdefault("parser_provenance", parsed_pdf.parser_provenance)
                return prepared_questions
        prepared_chunks = self._prepare_structured_chunks(document, parsed_pdf, asset_map)
        if prepared_chunks:
            return prepared_chunks
        fallback_chunks = self.rag_service.prepare_document_chunks(document, parsed_pdf.text, assets=parsed_pdf.assets, parsed_pdf=None)
        for chunk in fallback_chunks:
            chunk.metadata.setdefault("parser_backend", parsed_pdf.parser_backend)
            chunk.metadata.setdefault("parser_provenance", parsed_pdf.parser_provenance)
        return fallback_chunks

    def _prepare_structured_chunks(
        self,
        document: KnowledgeDocument,
        parsed_pdf: PDFParseResult,
        asset_map: dict[str, ExtractedAsset],
    ) -> list["PreparedChunk"]:
        structure_catalog = self._build_structure_catalog(document, parsed_pdf)
        current_chapter = document.chapter
        current_section = document.section
        current_structure_source = "document_metadata" if current_chapter or current_section else None
        current_structure_confidence = "low" if current_structure_source else None
        current_entries: list[dict[str, Any]] = []
        prepared: list[PreparedChunk] = []

        def flush_segment() -> None:
            nonlocal current_entries
            if not current_entries:
                return
            for chunk_entry in self._chunk_block_entries(current_entries):
                content, asset_refs = self.rag_service._finalize_chunk_text_and_assets(chunk_entry["text"], asset_map)
                merged_asset_refs = self._merge_asset_refs(chunk_entry["asset_refs"], asset_refs)
                if not content.strip():
                    continue
                page_start, page_end, source_pages = self._page_metadata(chunk_entry["pages"])
                prepared.append(
                    self.rag_service.PreparedChunk(
                        content=content,
                        metadata=self.rag_service._build_chunk_metadata(
                            document,
                            chapter=current_chapter,
                            section=current_section,
                            extra_metadata={
                                "tags": self._chunk_tags(document, current_chapter, current_section),
                                "contains_images": bool(merged_asset_refs),
                                "asset_refs": merged_asset_refs,
                                "image_count": len(merged_asset_refs),
                                "page_start": page_start,
                                "page_end": page_end,
                                "source_pages": source_pages,
                                "source_block_types": sorted(item for item in chunk_entry["block_types"] if item),
                                "structure_path": [item for item in (current_chapter, current_section) if str(item or "").strip()],
                                "structure_source": current_structure_source,
                                "structure_confidence": current_structure_confidence,
                                "parser_backend": parsed_pdf.parser_backend,
                                "parser_provenance": parsed_pdf.parser_provenance,
                                "toc_page_offset": structure_catalog.get("page_offset"),
                            },
                        ),
                    )
                )
            current_entries = []

        for block in parsed_pdf.blocks:
            block_text = (block.text or "").strip()
            if not block_text and not block.asset_id:
                continue
            page_number = block.page_index + 1
            if page_number in structure_catalog["toc_pages"]:
                continue

            page_structure = self._page_structure_context(structure_catalog, page_number)
            if page_structure and (
                page_structure.get("chapter") != current_chapter or page_structure.get("section") != current_section
            ):
                flush_segment()
                current_chapter = page_structure.get("chapter")
                current_section = page_structure.get("section")
                current_structure_source = str(page_structure.get("structure_source") or "toc_page_map")
                current_structure_confidence = str(page_structure.get("structure_confidence") or "medium")

            heading_context = self._resolve_heading_context(
                document,
                structure_catalog,
                block_text,
                str(block.block_type or ""),
            )
            if heading_context["chapter"]:
                flush_segment()
                current_chapter = heading_context["chapter"]
                current_section = None
                current_structure_source = str(heading_context.get("structure_source") or "body_heading")
                current_structure_confidence = str(heading_context.get("structure_confidence") or "high")
                continue
            if heading_context["section"]:
                flush_segment()
                current_section = heading_context["section"]
                current_structure_source = str(heading_context.get("structure_source") or "body_heading")
                current_structure_confidence = str(heading_context.get("structure_confidence") or "high")
                continue
            asset_refs: list[dict[str, Any]] = []
            if block.asset_id and block.asset_id in asset_map:
                self._append_asset_ref(asset_refs, self.rag_service._asset_payload(asset_map[block.asset_id]))
            current_entries.append(
                {
                    "text": block_text,
                    "asset_refs": asset_refs,
                    "page": block.page_index + 1,
                    "block_type": str(block.block_type or ""),
                    "metadata": dict(block.metadata or {}),
                }
            )

        flush_segment()
        return prepared

    def _prepare_question_chunks_from_blocks(
        self,
        document: KnowledgeDocument,
        parsed_pdf: PDFParseResult,
        asset_map: dict[str, ExtractedAsset],
    ) -> list["PreparedChunk"]:
        from backend.services.rag_service import QUESTION_SECTION_HEADING_PATTERN, QUESTION_START_PATTERN

        _, answer_bank = self.rag_service._split_question_and_answer_sections(parsed_pdf.text)
        answer_lookup = self.rag_service._parse_answer_bank(answer_bank or "")
        current_chapter = document.chapter
        current_section = document.section
        current_question: dict[str, Any] | None = None
        prepared: list[PreparedChunk] = []
        in_answer_bank = False

        def flush_question() -> None:
            nonlocal current_question
            if not current_question:
                return
            block_text = "\n".join(current_question["lines"]).strip()
            if not block_text:
                current_question = None
                return
            question_body, local_answer, local_explanation = self.rag_service._split_question_block_sections(block_text)
            number = current_question["number"]
            merged_answer = local_answer or answer_lookup.get(number, {}).get("answer_text")
            merged_explanation = local_explanation or answer_lookup.get(number, {}).get("explanation_text")
            combined_text = self.rag_service._compose_question_chunk_text(
                number=number,
                question_text=question_body,
                answer_text=merged_answer,
                explanation_text=merged_explanation,
            )
            finalized_text, chunk_asset_refs = self.rag_service._finalize_chunk_text_and_assets(combined_text, asset_map)
            clean_question_text, question_assets = self.rag_service._finalize_chunk_text_and_assets(question_body, asset_map)
            clean_answer_text, _ = self.rag_service._finalize_chunk_text_and_assets(merged_answer or "", asset_map)
            clean_explanation_text, _ = self.rag_service._finalize_chunk_text_and_assets(merged_explanation or "", asset_map)
            merged_asset_refs = self._merge_asset_refs(current_question["asset_refs"], question_assets, chunk_asset_refs)
            page_start, page_end, source_pages = self._page_metadata(current_question["pages"])
            prepared.append(
                self.rag_service._build_question_bank_chunk(
                    document,
                    content=finalized_text,
                    question_number=number,
                    question_text=clean_question_text or finalized_text,
                    answer_text=clean_answer_text or None,
                    explanation_text=clean_explanation_text or None,
                    asset_refs=merged_asset_refs,
                    chapter=current_question["chapter"],
                    section=current_question["section"],
                    tags=self._chunk_tags(document, current_question["chapter"], current_question["section"]),
                    structure_path=[
                        item
                        for item in (current_question["chapter"], current_question["section"])
                        if str(item or "").strip()
                    ],
                    source_format="pdf",
                    parser_backend=parsed_pdf.parser_backend,
                    parser_provenance=parsed_pdf.parser_provenance,
                    page_start=page_start,
                    page_end=page_end,
                    source_pages=source_pages,
                    source_block_types=sorted(item for item in current_question["block_types"] if item),
                )
            )
            current_question = None

        for block in parsed_pdf.blocks:
            block_text = (block.text or "").strip()
            heading_context = self.rag_service._extract_heading_context(block_text, document.resource_type)
            if heading_context["chapter"]:
                flush_question()
                current_chapter = heading_context["chapter"]
                current_section = None
                continue
            if heading_context["section"]:
                flush_question()
                current_section = heading_context["section"]
                continue
            if block_text and QUESTION_SECTION_HEADING_PATTERN.match(block_text):
                flush_question()
                in_answer_bank = True
                continue
            if in_answer_bank:
                continue
            match = QUESTION_START_PATTERN.match(block_text) if block_text else None
            if match:
                flush_question()
                number = self.rag_service._question_number_from_match(match)
                body = (match.group("body") or "").strip()
                current_question = {
                    "number": number,
                    "chapter": current_chapter,
                    "section": current_section,
                    "lines": [body] if body else [],
                    "asset_refs": [],
                    "pages": {block.page_index + 1},
                    "block_types": {str(block.block_type)} if block.block_type else set(),
                }
                if block.asset_id and block.asset_id in asset_map:
                    self._append_asset_ref(
                        current_question["asset_refs"],
                        self.rag_service._asset_payload(asset_map[block.asset_id]),
                    )
                continue
            if not current_question:
                continue
            current_question["pages"].add(block.page_index + 1)
            if block.block_type:
                current_question["block_types"].add(str(block.block_type))
            if block.asset_id and block.asset_id in asset_map:
                self._append_asset_ref(
                    current_question["asset_refs"],
                    self.rag_service._asset_payload(asset_map[block.asset_id]),
                )
            if block_text:
                current_question["lines"].append(block_text)

        flush_question()
        return prepared

    def _clean_pdf_parse_result(self, document: KnowledgeDocument, parsed_pdf: PDFParseResult) -> PDFParseResult:
        normalized_blocks = [
            replace(
                block,
                text=self._strip_bound_asset_path_noise(
                    self._normalize_formula_text(self._normalize_markup_text(block.text)),
                    asset_bound=bool(block.asset_id) or "[[asset:" in str(block.text or ""),
                ),
                metadata=dict(block.metadata or {}),
            )
            for block in parsed_pdf.blocks
        ]
        suppressed = self._boilerplate_suppression_keys(document, normalized_blocks)
        cleaned_blocks: list[PDFBlock] = []
        for block in normalized_blocks:
            block_text = str(block.text or "").strip()
            if not block_text:
                continue
            if block_text.lower() == "latex" and cleaned_blocks and self._is_wrapped_formula(str(cleaned_blocks[-1].text or "").strip()):
                continue
            if self._looks_like_formula_image_path(block_text) and cleaned_blocks and self._is_wrapped_formula(str(cleaned_blocks[-1].text or "").strip()):
                continue
            if self._boilerplate_key(block_text) in suppressed and not self._is_protected_pdf_block(document, block):
                continue
            cleaned_blocks.append(replace(block, text=block_text))
        cleaned_text = "\n\n".join(block.text.strip() for block in cleaned_blocks if block.text.strip()).strip()
        return replace(parsed_pdf, text=cleaned_text, blocks=cleaned_blocks)

    def _normalize_markup_text(self, text: str) -> str:
        raw_text = str(text or "").strip()
        if "<table" not in raw_text.lower() and "<tr" not in raw_text.lower() and "<td" not in raw_text.lower() and "<th" not in raw_text.lower():
            return raw_text
        normalized = re.sub(r"(?i)</tr\s*>", "\n", raw_text)
        normalized = re.sub(r"(?i)<tr\b[^>]*>", "", normalized)
        normalized = re.sub(r"(?i)</t[dh]\s*>", "\t", normalized)
        normalized = re.sub(r"(?i)<t[dh]\b[^>]*>", "", normalized)
        normalized = re.sub(r"(?i)</?table\b[^>]*>", "", normalized)
        normalized = re.sub(r"(?i)<br\s*/?>", "\n", normalized)
        normalized = re.sub(r"<[^>]+>", "", normalized)
        lines: list[str] = []
        for raw_line in normalized.splitlines():
            cells = [cell.strip() for cell in raw_line.split("\t")]
            if not any(cells):
                continue
            non_empty = [cell for cell in cells if cell]
            lines.append(" | ".join(non_empty) if non_empty else "")
        return "\n".join(line for line in lines if line).strip()

    def _normalize_formula_text(self, text: str) -> str:
        raw_text = str(text or "").strip()
        if not raw_text:
            return ""

        lines = [line.strip() for line in raw_text.splitlines()]
        normalized_lines: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            lowered = line.lower()
            next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            next_next_line = lines[index + 2].strip() if index + 2 < len(lines) else ""

            if lowered in {"equation_inline", "equation_display"}:
                display_mode = lowered == "equation_display"
                if next_line and self._looks_like_equation_marker_payload(next_line):
                    normalized_lines.append(self._wrap_formula_text(next_line, display_mode=display_mode))
                    index += 2
                    continue
                normalized_lines.append(line)
                index += 1
                continue

            if lowered == "latex":
                if normalized_lines and self._is_wrapped_formula(normalized_lines[-1]):
                    if next_line and self._looks_like_formula_image_path(next_line):
                        index += 2
                        continue
                    index += 1
                    continue
                normalized_lines.append(line)
                index += 1
                continue

            if self._looks_like_formula_image_path(line):
                previous_line = normalized_lines[-1] if normalized_lines else ""
                if previous_line and self._is_wrapped_formula(previous_line):
                    index += 1
                    continue
                normalized_lines.append(line)
                index += 1
                continue

            if next_line.lower() == "latex" and self._looks_like_formula_payload(line):
                display_mode = self._prefers_display_formula(line)
                normalized_lines.append(self._wrap_formula_text(line, display_mode=display_mode))
                index += 2
                if next_next_line and self._looks_like_formula_image_path(next_next_line):
                    index += 1
                continue

            if next_line.lower() == "latex" and next_next_line and self._looks_like_formula_image_path(next_next_line) and self._looks_like_formula_payload(line):
                display_mode = self._prefers_display_formula(line)
                normalized_lines.append(self._wrap_formula_text(line, display_mode=display_mode))
                index += 3
                continue

            if self._looks_like_formula_payload(line):
                normalized_lines.append(self._wrap_formula_text(line, display_mode=self._prefers_display_formula(line)))
                index += 1
                continue

            normalized_lines.append(line)
            index += 1

        collapsed: list[str] = []
        previous_blank = False
        for line in normalized_lines:
            if not line:
                if not previous_blank:
                    collapsed.append("")
                previous_blank = True
                continue
            collapsed.append(line)
            previous_blank = False
        return "\n".join(collapsed).strip()

    def _wrap_formula_text(self, text: str, *, display_mode: bool) -> str:
        normalized = self._repair_formula_spacing(text)
        if self._is_wrapped_formula(normalized):
            return normalized
        delimiter = "$$" if display_mode else "$"
        return f"{delimiter}{normalized}{delimiter}"

    def _repair_formula_spacing(self, text: str) -> str:
        normalized = str(text or "").strip()
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"\\(begin|end)\s*\{\s*([A-Za-z*]+)\s*\}", r"\\\1{\2}", normalized)
        normalized = re.sub(r"\s*_\s*\{\s*", "_{", normalized)
        normalized = re.sub(r"\s*\^\s*\{\s*", "^{", normalized)
        normalized = re.sub(r"\}\s+\{", "}{", normalized)
        normalized = re.sub(r"\s+\}", "}", normalized)
        normalized = re.sub(r"\\([A-Za-z]+)\s+\{", r"\\\1{", normalized)
        normalized = re.sub(r"\{\s+", "{", normalized)
        normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)
        normalized = re.sub(r"(?<=\d)\s+(?=\d)", "", normalized)
        normalized = re.sub(r"(?<!\d)1\s+0(?=\s*\^\s*\{)", "10", normalized)
        normalized = re.sub(r"(?<=\d)\s+\^(?=\s*\{)", "^", normalized)
        normalized = re.sub(r"\^\{\s*-\s*(\d+)\s*\}", r"^{-\1}", normalized)
        normalized = re.sub(r"\^\{\s*(\d+)\s*\}", r"^{\1}", normalized)
        normalized = re.sub(r"\\mathrm\{([^}]*)\}", lambda m: "\\mathrm{" + re.sub(r"\s+", "", m.group(1)) + "}", normalized)
        normalized = re.sub(r"\\scriptscriptstyle\s+", r"\\scriptscriptstyle ", normalized)
        return normalized.strip()

    def _looks_like_equation_marker_payload(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False
        if self._looks_like_formula_payload(candidate):
            return True
        if re.search(r"\\[A-Za-z]+", candidate) and not re.search(r"[\u4e00-\u9fff]{2,}", candidate):
            return True
        return False

    def _looks_like_formula_payload(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False
        if candidate.startswith("[[asset:") or candidate.startswith("【附图"):
            return False
        if self._looks_like_formula_image_path(candidate):
            return False
        if self._is_wrapped_formula(candidate):
            return False
        if re.match(r"^\s*(?:\d+\.\s*)?(?:答案|解析)[:：]", candidate):
            return False
        if re.search(r"\\begin\{array\}|\\end\{array\}", candidate):
            return True
        if re.search(r"\\[A-Za-z]+", candidate) and not re.search(r"[\u4e00-\u9fff]{2,}", candidate):
            return True
        if re.search(r"\\(?:frac|mathrm|scriptscriptstyle|sin|cos|tan|theta|pi|perp|ast|times|begin|end)", candidate):
            return True
        if re.search(r"(?:^| )equation_inline(?:$| )", candidate, re.I):
            return False
        if re.search(r"(?:_|\^)\s*\{", candidate):
            return True
        if re.search(r"\d\s*\.\s*\d|\b1\s+0\s*\^\s*\{", candidate) and not re.search(r"[\u4e00-\u9fff]", candidate):
            return True
        if re.search(r"=", candidate) and not re.search(r"[\u4e00-\u9fff]{2,}", candidate):
            return True
        return False

    def _prefers_display_formula(self, text: str) -> bool:
        candidate = str(text or "").strip()
        return bool(re.search(r"\\begin\{array\}|\\\\|\\frac", candidate))

    def _looks_like_formula_image_path(self, text: str) -> bool:
        return bool(re.fullmatch(r"images?/[^ \n]+\.(?:png|jpg|jpeg|webp)", str(text or "").strip(), re.I))

    def _strip_bound_asset_path_noise(self, text: str, *, asset_bound: bool) -> str:
        if not asset_bound:
            return str(text or "").strip()
        cleaned_lines = [
            line
            for line in (str(text or "").splitlines())
            if not self._looks_like_formula_image_path(line.strip())
        ]
        return "\n".join(line for line in cleaned_lines if line.strip()).strip()

    def _is_wrapped_formula(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False
        return (
            (candidate.startswith("$$") and candidate.endswith("$$"))
            or (candidate.startswith("$") and candidate.endswith("$") and candidate.count("$") >= 2)
            or (candidate.startswith(r"\(") and candidate.endswith(r"\)"))
            or (candidate.startswith(r"\[") and candidate.endswith(r"\]"))
        )

    def _boilerplate_suppression_keys(self, document: KnowledgeDocument, blocks: list[PDFBlock]) -> set[str]:
        page_blocks: dict[int, list[PDFBlock]] = {}
        for block in blocks:
            block_text = str(block.text or "").strip()
            if not block_text:
                continue
            page_blocks.setdefault(block.page_index + 1, []).append(block)
        if not page_blocks:
            return set()

        candidate_pages: dict[str, set[int]] = {}
        for page_number, page_items in page_blocks.items():
            edge_indexes = {0, max(len(page_items) - 1, 0)}
            for index, block in enumerate(page_items):
                if index not in edge_indexes and not self._block_has_footer_signal(block):
                    continue
                if self._is_protected_pdf_block(document, block):
                    continue
                key = self._boilerplate_key(block.text)
                if not key:
                    continue
                candidate_pages.setdefault(key, set()).add(page_number)

        total_pages = len(page_blocks)
        required_count = max(2, total_pages if total_pages <= 3 else max(3, (total_pages + 1) // 2))
        return {key for key, pages in candidate_pages.items() if len(pages) >= required_count}

    def _block_has_footer_signal(self, block: PDFBlock) -> bool:
        roles = {str(item).strip() for item in (block.metadata or {}).get("content_roles", []) if str(item).strip()}
        return "page_footer_content" in roles

    def _is_protected_pdf_block(self, document: KnowledgeDocument | None, block: PDFBlock) -> bool:
        if str(block.block_type or "") == "title":
            return True
        roles = {str(item).strip() for item in (block.metadata or {}).get("content_roles", []) if str(item).strip()}
        if roles.intersection({"table_caption", "image_caption", "image_footnote", "algorithm_caption", "algorithm_footnote"}):
            return True
        resource_type = document.resource_type if document is not None else None
        heading_context = self.rag_service._extract_heading_context(str(block.text or ""), resource_type)
        return bool(heading_context.get("chapter") or heading_context.get("section"))

    def _boilerplate_key(self, text: str) -> str | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        normalized = re.sub(r"\s+", "", normalized)
        normalized = re.sub(r"[·•⋯…_\-–—=]+", "", normalized)
        return normalized or None

    def _chunk_tags(
        self,
        document: KnowledgeDocument,
        chapter: str | None,
        section: str | None,
    ) -> list[str]:
        tags = list(document.tags if hasattr(document, "tags") else [])
        for value in (chapter, section):
            normalized = str(value or "").strip()
            if normalized and normalized not in tags:
                tags.append(normalized)
        if "mineru-pdf" not in tags:
            tags.append("mineru-pdf")
        return tags

    def _append_asset_ref(self, asset_refs: list[dict[str, Any]], payload: dict[str, Any]) -> None:
        asset_id = str(payload.get("asset_id") or "")
        if asset_id and any(str(item.get("asset_id") or "") == asset_id for item in asset_refs):
            return
        asset_refs.append(payload)

    def _merge_asset_refs(self, *groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for group in groups:
            for item in group or []:
                self._append_asset_ref(merged, item)
        return merged

    def _build_structure_catalog(self, document: KnowledgeDocument, parsed_pdf: PDFParseResult) -> dict[str, Any]:
        resource_type = document.resource_type or ResourceType.KNOWLEDGE_NOTE.value
        if resource_type != ResourceType.TEXTBOOK.value:
            return {"toc_pages": set(), "entries": [], "page_offset": None}

        page_texts: dict[int, list[str]] = {}
        for block in parsed_pdf.blocks:
            page_number = block.page_index + 1
            block_text = str(block.text or "").strip()
            if not block_text:
                continue
            page_texts.setdefault(page_number, []).extend(line.strip() for line in block_text.splitlines() if line.strip())

        toc_pages = {
            page_number
            for page_number, lines in page_texts.items()
            if self._looks_like_toc_page(lines, resource_type)
        }

        entries: list[dict[str, Any]] = []
        for page_number in sorted(toc_pages):
            for line in page_texts.get(page_number, []):
                entry = self._parse_toc_entry(line, resource_type)
                if entry:
                    entries.append(entry)

        page_offset = self._infer_toc_page_offset(entries, parsed_pdf, toc_pages, resource_type)
        if page_offset is not None:
            for entry in entries:
                entry["pdf_page"] = entry["printed_page"] + page_offset

        return {"toc_pages": toc_pages, "entries": entries, "page_offset": page_offset}

    def _looks_like_toc_page(self, lines: list[str], resource_type: str) -> bool:
        if any(line in {"目录", "目 录"} for line in lines):
            return True
        toc_like_entries = sum(1 for line in lines if self._parse_toc_entry(line, resource_type))
        return toc_like_entries >= 2

    def _parse_toc_entry(self, line: str, resource_type: str) -> dict[str, Any] | None:
        stripped = str(line or "").strip()
        if not stripped or stripped in {"目录", "目 录"}:
            return None
        match = re.match(r"^(?P<title>.+?)(?:[\s.．·•⋯…_-]{2,}|\s+)(?P<page>\d{1,4})$", stripped)
        if not match:
            return None
        title = self._strip_toc_page_marker(str(match.group("title") or "").strip())
        heading_context = self.rag_service._extract_heading_context(title, resource_type)
        resolved_title = heading_context["chapter"] or heading_context["section"]
        if not resolved_title:
            return None
        return {
            "kind": "chapter" if heading_context["chapter"] else "section",
            "title": resolved_title,
            "key": self.rag_service._structure_key(resolved_title),
            "printed_page": int(match.group("page")),
        }

    def _strip_toc_page_marker(self, text: str) -> str:
        return re.sub(r"[\s.．·•⋯…_-]+$", "", str(text or "").strip())

    def _infer_toc_page_offset(
        self,
        entries: list[dict[str, Any]],
        parsed_pdf: PDFParseResult,
        toc_pages: set[int],
        resource_type: str,
    ) -> int | None:
        if not entries:
            return None
        entry_map = {
            str(entry.get("key")): entry
            for entry in entries
            if str(entry.get("key") or "").strip()
        }
        offsets: list[int] = []
        for block in parsed_pdf.blocks:
            page_number = block.page_index + 1
            if page_number in toc_pages:
                continue
            heading_context = self.rag_service._extract_heading_context(block.text or "", resource_type)
            for title in (heading_context.get("chapter"), heading_context.get("section")):
                key = self.rag_service._structure_key(str(title or ""))
                if not key or key not in entry_map:
                    continue
                offsets.append(page_number - int(entry_map[key]["printed_page"]))
        if not offsets:
            return None
        return Counter(offsets).most_common(1)[0][0]

    def _resolve_heading_context(
        self,
        document: KnowledgeDocument,
        structure_catalog: dict[str, Any],
        block_text: str,
        block_type: str,
    ) -> dict[str, Any]:
        heading_context = self.rag_service._extract_heading_context(block_text, document.resource_type)
        if heading_context["chapter"]:
            normalized = self._normalize_heading_from_catalog(structure_catalog, str(heading_context["chapter"]))
            return {
                "chapter": normalized,
                "section": None,
                "structure_source": "body_heading_normalized" if normalized != heading_context["chapter"] else "body_heading",
                "structure_confidence": "high",
            }
        if heading_context["section"]:
            section_title = str(heading_context["section"])
            section_key = self.rag_service._structure_key(section_title)
            catalog_keys = {
                str(entry.get("key"))
                for entry in structure_catalog.get("entries", [])
                if entry.get("kind") == "section" and str(entry.get("key") or "").strip()
            }
            if (
                (document.resource_type or ResourceType.KNOWLEDGE_NOTE.value) == ResourceType.TEXTBOOK.value
                and block_type != "title"
                and section_key not in catalog_keys
            ):
                return {"chapter": None, "section": None}
            normalized = self._normalize_heading_from_catalog(structure_catalog, str(heading_context["section"]))
            return {
                "chapter": None,
                "section": normalized,
                "structure_source": "body_heading_normalized" if normalized != heading_context["section"] else "body_heading",
                "structure_confidence": "high",
            }
        return {"chapter": None, "section": None}

    def _normalize_heading_from_catalog(self, structure_catalog: dict[str, Any], title: str) -> str:
        title_key = self.rag_service._structure_key(title)
        if not title_key:
            return title
        for entry in structure_catalog.get("entries", []):
            if entry.get("key") == title_key and entry.get("title"):
                return str(entry["title"])
        return title

    def _page_structure_context(self, structure_catalog: dict[str, Any], page_number: int) -> dict[str, Any] | None:
        entries = [
            entry
            for entry in structure_catalog.get("entries", [])
            if isinstance(entry.get("pdf_page"), int) and int(entry["pdf_page"]) <= page_number
        ]
        if not entries:
            return None
        chapter: str | None = None
        section: str | None = None
        for entry in sorted(entries, key=lambda item: (int(item["pdf_page"]), 0 if item["kind"] == "chapter" else 1)):
            if entry["kind"] == "chapter":
                chapter = str(entry["title"])
                section = None
            else:
                section = str(entry["title"])
        if not chapter and not section:
            return None
        return {
            "chapter": chapter,
            "section": section,
            "structure_source": "toc_page_map",
            "structure_confidence": "medium",
        }

    def _page_metadata(self, pages: set[int]) -> tuple[int | None, int | None, list[int]]:
        source_pages = sorted(page for page in pages if isinstance(page, int) and page > 0)
        if not source_pages:
            return None, None, []
        return source_pages[0], source_pages[-1], source_pages

    def _chunk_block_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        current_texts: list[str] = []
        current_asset_refs: list[dict[str, Any]] = []
        current_pages: set[int] = set()
        current_block_types: set[str] = set()

        def flush_current() -> None:
            nonlocal current_texts, current_asset_refs, current_pages, current_block_types
            text = "\n".join(part for part in current_texts if str(part or "").strip()).strip()
            if not text:
                current_texts = []
                current_asset_refs = []
                current_pages = set()
                current_block_types = set()
                return
            chunks.append(
                {
                    "text": text,
                    "asset_refs": current_asset_refs[:],
                    "pages": set(current_pages),
                    "block_types": set(current_block_types),
                }
            )
            current_texts = []
            current_asset_refs = []
            current_pages = set()
            current_block_types = set()

        for entry in entries:
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            page = entry.get("page")
            block_type = str(entry.get("block_type") or "")
            entry_asset_refs = list(entry.get("asset_refs") or [])
            text_parts = self.rag_service.split_text(text) if len(text) > self.rag_service.settings.rag_chunk_size else [text]
            appended_assets = False

            for part in text_parts:
                normalized = str(part or "").strip()
                if not normalized:
                    continue
                candidate = "\n".join(current_texts + [normalized]).strip()
                if current_texts and len(candidate) > self.rag_service.settings.rag_chunk_size:
                    flush_current()
                current_texts.append(normalized)
                if not appended_assets:
                    current_asset_refs = self._merge_asset_refs(current_asset_refs, entry_asset_refs)
                    appended_assets = True
                if isinstance(page, int) and page > 0:
                    current_pages.add(page)
                if block_type:
                    current_block_types.add(block_type)

        flush_current()
        return chunks
