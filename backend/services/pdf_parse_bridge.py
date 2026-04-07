from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.models.knowledge import KnowledgeDocument, ResourceType
from backend.services.pdf_parse_types import ExtractedAsset, PDFParseResult

if TYPE_CHECKING:
    from backend.services.rag_service import PreparedChunk, RagService


class PDFParseBridge:
    def __init__(self, rag_service: "RagService") -> None:
        self.rag_service = rag_service

    def prepare_chunks(self, document: KnowledgeDocument, parsed_pdf: PDFParseResult) -> list["PreparedChunk"]:
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
        current_chapter = document.chapter
        current_section = document.section
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
                                "parser_backend": parsed_pdf.parser_backend,
                                "parser_provenance": parsed_pdf.parser_provenance,
                            },
                        ),
                    )
                )
            current_entries = []

        for block in parsed_pdf.blocks:
            block_text = (block.text or "").strip()
            if not block_text and not block.asset_id:
                continue
            heading_context = self.rag_service._extract_heading_context(block_text, document.resource_type)
            if heading_context["chapter"]:
                flush_segment()
                current_chapter = heading_context["chapter"]
                current_section = None
                continue
            if heading_context["section"]:
                flush_segment()
                current_section = heading_context["section"]
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
