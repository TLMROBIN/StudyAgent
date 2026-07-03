"""Question-bank chunk building: question/answer parsing, grouping and chunk composition.

Moved verbatim from backend.services.rag_service.RagService.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import re
from typing import Any

from backend.models.knowledge import KnowledgeDocument
from backend.services.chunking.models import PreparedChunk
from backend.services.pdf_parse_types import ExtractedAsset
from backend.services.question_bank_post_processor import QuestionBankChunkCandidate


@dataclass
class QuestionChunkDraft:
    question_number: str
    question_text: str
    answer_text: str | None = None
    explanation_text: str | None = None
    asset_refs: list[dict[str, Any]] = field(default_factory=list)


QUESTION_START_PATTERN = re.compile(
    r"^\s*(?:"
    r"第\s*(?P<ordinal>\d+)\s*题"
    r"|(?P<plain>\d{1,3})(?:\s*[.．、)]|\s*[:：](?!\s*\d))"
    r"|[（(](?P<wrapped>\d{1,3})[)）]"
    r")\s*(?P<body>.*)$"
)
QUESTION_SECTION_HEADING_PATTERN = re.compile(
    r"^\s*(?:【)?(?:参考)?(?:答案(?:与解析)?|答案及解析|解答|参考解答|参考解析|解析|详解)(?:】)?\s*$"
)
ANSWER_LINE_PATTERN = re.compile(r"^\s*(?:【)?(?:参考)?答案(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
EXPLANATION_LINE_PATTERN = re.compile(r"^\s*(?:【)?(?:解析|详解|解答|思路(?:点拨)?|点拨|说明|分析|点睛)(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
INLINE_EXPLANATION_SEGMENT_PATTERN = re.compile(
    r"^(?P<answer>.+?)\s+(?P<explanation>(?:【)?(?:解析|详解|解答|思路(?:点拨)?|点拨|说明|分析|点睛)(?:】)?\s*(?:[:：]\s*)?.+)$"
)
ANSWER_ONLY_TOKEN_PATTERN = re.compile(r"^(?:[A-H](?:\s*[、,，/]\s*[A-H]){0,7}|[√×]|正确|错误|对|错|T|F|\d+(?:\.\d+)?)$")
DIFFICULTY_LINE_PATTERN = re.compile(r"^\s*(?:【)?难度(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
KNOWLEDGE_POINTS_LINE_PATTERN = re.compile(r"^\s*(?:【)?知识点(?:】)?\s*(?:[:：]\s*)?(?P<body>.*)?$")
QUESTION_CATEGORY_HEADING_PATTERN = re.compile(
    r"^\s*[一二三四五六七八九十]+[、.．]\s*(?:单选题|多选题|填空题|选择题|判断题|解答题|计算题|实验题|综合题|简答题)\s*$"
)


class QuestionChunkBuilderMixin:
    """Mixin providing question-bank parsing and chunk composition for RagService."""

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
        prepared_chunk_records: list[tuple[int, int, PreparedChunk]] = []
        order = 0
        for group_id, (question_text, answer_bank) in enumerate(self._split_question_groups(normalized_text or text)):
            question_blocks = self._parse_numbered_blocks(
                question_text,
                keep_wrapped_subquestions=True,
            )
            question_blocks = [
                (number, block_text)
                for number, block_text in question_blocks
                if not self._looks_like_exam_cover_block(block_text)
            ]
            if not question_blocks:
                continue

            expected_numbers = [number for number, _ in question_blocks]
            answer_lookup = self._parse_answer_bank(answer_bank or "", expected_numbers=expected_numbers)
            question_blocks, repeated_answer_lookup = self._merge_repeated_answer_bank_blocks(
                question_blocks
            )
            single_group_answer: str | None = None
            single_group_explanation: str | None = None
            if len(question_blocks) == 1 and str(answer_bank or "").strip():
                single_group_answer, single_group_explanation = self._split_grouped_answer_bank_sections(
                    answer_bank or ""
                )

            if self._should_group_question_blocks(question_blocks, answer_lookup, answer_bank):
                grouped_chunk = self._build_grouped_question_bank_chunk(
                    document,
                    question_text=question_text,
                    answer_bank=answer_bank or "",
                    question_blocks=question_blocks,
                    asset_map=asset_map,
                    source_format=source_format,
                    parser_backend=parser_backend,
                    parser_provenance=parser_provenance,
                )
                if grouped_chunk is not None:
                    prepared_chunk_records.append((group_id, order, grouped_chunk))
                    order += 1
                    continue

            for number, block_text in question_blocks:
                question_body, local_answer, local_explanation = self._split_question_block_sections(block_text)
                raw_metadata_text = block_text
                if len(question_blocks) == 1 and str(answer_bank or "").strip():
                    raw_metadata_text = "\n".join(part for part in (block_text, answer_bank or "") if str(part).strip())
                merged_answer = (
                    local_answer
                    or single_group_answer
                    or answer_lookup.get(number, {}).get("answer_text")
                    or repeated_answer_lookup.get(number, {}).get("answer_text")
                )
                merged_explanation = (
                    local_explanation
                    or single_group_explanation
                    or answer_lookup.get(number, {}).get("explanation_text")
                    or repeated_answer_lookup.get(number, {}).get("explanation_text")
                )
                merged_answer = self._strip_matching_question_number_prefix(merged_answer, number)
                merged_explanation = self._strip_matching_question_number_prefix(merged_explanation, number)
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
                chunk = self._build_question_bank_chunk(
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
                        raw_block_text=raw_metadata_text,
                )
                prepared_chunk_records.append((group_id, order, chunk))
                order += 1
        return self._reconcile_prepared_question_chunks(document, prepared_chunk_records)

    def _reconcile_prepared_question_chunks(
        self,
        document: KnowledgeDocument,
        chunk_records: list[tuple[int, int, PreparedChunk]],
    ) -> list[PreparedChunk]:
        grouped_records: dict[tuple[str, int], list[tuple[int, PreparedChunk]]] = {}
        passthrough_records: list[tuple[int, PreparedChunk]] = []
        first_seen_order: dict[tuple[str, int], int] = {}

        for group_id, order, chunk in chunk_records:
            metadata = chunk.metadata or {}
            if metadata.get("chunk_kind") != "question_item":
                passthrough_records.append((order, chunk))
                continue
            question_number = str(metadata.get("question_number") or "").strip()
            if not question_number:
                passthrough_records.append((order, chunk))
                continue
            key = (question_number, group_id)
            grouped_records.setdefault(key, []).append((order, chunk))
            first_seen_order.setdefault(key, order)

        merged_records: list[tuple[int, PreparedChunk]] = passthrough_records[:]
        for key, items in grouped_records.items():
            ordered_items = sorted(items, key=lambda item: item[0])
            if len(ordered_items) == 1:
                merged_records.append((ordered_items[0][0], ordered_items[0][1]))
                continue
            merged_records.append(
                (
                    first_seen_order[key],
                    self._merge_question_chunk_group(document, [chunk for _, chunk in ordered_items]),
                )
            )

        merged_records.sort(key=lambda item: item[0])
        reconciled_chunks = [chunk for _, chunk in merged_records]
        self._apply_question_locator_collision_suffixes(document, reconciled_chunks)
        return reconciled_chunks

    def _merge_question_chunk_group(
        self,
        document: KnowledgeDocument,
        chunks: list[PreparedChunk],
    ) -> PreparedChunk:
        primary = chunks[0]
        primary_metadata = dict(primary.metadata or {})

        question_text = self._merge_question_texts(
            [chunk.metadata.get("question_text") for chunk in chunks if chunk.metadata]
        )
        answer_text = self._merge_answer_texts(
            [chunk.metadata.get("answer_text") for chunk in chunks if chunk.metadata]
        )
        explanation_text = self._merge_explanation_texts(
            [chunk.metadata.get("explanation_text") for chunk in chunks if chunk.metadata]
        )
        asset_refs = self.question_bank_post_processor._dedupe_asset_refs(
            [
                asset
                for chunk in chunks
                for asset in list((chunk.metadata or {}).get("asset_refs") or [])
            ]
        )

        question_number = str(primary_metadata.get("question_number") or "").strip()
        merged_content = self._compose_question_chunk_text(
            number=question_number,
            question_text=question_text,
            answer_text=answer_text,
            explanation_text=explanation_text,
        )

        primary_metadata["question_text"] = question_text or None
        primary_metadata["answer_text"] = answer_text
        primary_metadata["explanation_text"] = explanation_text
        primary_metadata["asset_refs"] = asset_refs
        primary_metadata["image_count"] = len(asset_refs)
        primary_metadata["contains_images"] = bool(asset_refs)
        image_expectation = str(primary_metadata.get("image_expectation") or "not_needed")
        primary_metadata["image_binding_status"] = self.question_bank_post_processor._image_binding_status(
            image_expectation,
            asset_refs,
        )
        primary_metadata["quality_flags"] = self.question_bank_post_processor._quality_flags(
            question_text=question_text,
            answer_text=answer_text,
            explanation_text=explanation_text,
            image_binding_status=str(primary_metadata.get("image_binding_status") or "none_needed"),
        )
        source_locator = str(primary_metadata.get("source_locator") or f"question:{question_number}").strip()
        primary_metadata["source_locator"] = source_locator
        primary_metadata["question_uid"] = f"qb:{document.id}:{source_locator or question_number or 'unknown'}"

        return PreparedChunk(content=merged_content, metadata=primary_metadata)

    def _merge_question_texts(self, texts: list[str | None]) -> str:
        valid = [str(text).strip() for text in texts if str(text or "").strip()]
        if not valid:
            return ""
        return max(valid, key=len)

    def _merge_answer_texts(self, texts: list[str | None]) -> str | None:
        for text in texts:
            normalized = str(text or "").strip()
            if normalized:
                return normalized
        return None

    def _merge_explanation_texts(self, texts: list[str | None]) -> str | None:
        valid = [str(text).strip() for text in texts if str(text or "").strip()]
        if not valid:
            return None
        deduped: list[str] = []
        for text in valid:
            if any(text == existing for existing in deduped):
                continue
            if any(text in existing for existing in deduped):
                continue
            deduped = [existing for existing in deduped if existing not in text]
            deduped.append(text)
        return "\n\n".join(deduped)

    def _apply_question_locator_collision_suffixes(
        self,
        document: KnowledgeDocument,
        chunks: list[PreparedChunk],
    ) -> None:
        numbered_chunks: dict[str, list[PreparedChunk]] = defaultdict(list)
        for chunk in chunks:
            metadata = chunk.metadata or {}
            if metadata.get("chunk_kind") != "question_item":
                continue
            question_number = str(metadata.get("question_number") or "").strip()
            if question_number:
                numbered_chunks[question_number].append(chunk)

        for question_number, question_chunks in numbered_chunks.items():
            if len(question_chunks) <= 1:
                continue
            for index, chunk in enumerate(question_chunks, start=1):
                metadata = chunk.metadata or {}
                metadata["source_locator"] = f"question:{question_number}|v:{index}"
                metadata["question_uid"] = f"qb:{document.id}:{metadata['source_locator']}"

    def _strip_matching_question_number_prefix(
        self,
        text: str | None,
        question_number: str,
    ) -> str | None:
        normalized = str(text or "").strip()
        if not normalized or not question_number:
            return normalized or None
        pattern = re.compile(
            rf"^\s*(?:"
            rf"第\s*{re.escape(question_number)}\s*题"
            rf"|{re.escape(question_number)}(?:\s*[、)]|\s*[.．](?!\s*\d)|\s*[:：](?!\s*\d))"
            rf")\s*"
        )
        stripped = pattern.sub("", normalized, count=1).strip()
        return stripped or normalized or None

    def _split_question_groups(self, text: str) -> list[tuple[str, str | None]]:
        lines = [line.rstrip() for line in str(text or "").split("\n")]
        groups: list[tuple[str, str | None]] = []
        question_lines: list[str] = []
        answer_lines: list[str] = []
        mode = "question"
        answer_mode = "answer"
        question_count = 0
        current_question_numbers: list[int] = []

        def flush() -> None:
            nonlocal question_lines, answer_lines, mode, answer_mode, question_count, current_question_numbers
            question_text = "\n".join(question_lines).strip()
            answer_text = "\n".join(answer_lines).strip() or None
            if question_text:
                groups.append((question_text, answer_text))
            question_lines = []
            answer_lines = []
            mode = "question"
            answer_mode = "answer"
            question_count = 0
            current_question_numbers = []

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                target = question_lines if mode == "question" else answer_lines
                if target:
                    target.append("")
                continue

            question_match = QUESTION_START_PATTERN.match(stripped)
            if mode == "question":
                answer_match = ANSWER_LINE_PATTERN.match(stripped)
                explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
                if question_count >= 1 and (
                    QUESTION_SECTION_HEADING_PATTERN.match(stripped)
                    or answer_match
                    or explanation_match
                ):
                    mode = "answer"
                    answer_mode = "explanation" if explanation_match else "answer"
                    answer_lines.append(stripped)
                    continue
                question_lines.append(stripped)
                if question_match and (question_match.group("ordinal") or question_match.group("plain") or question_match.group("wrapped")):
                    question_count += 1
                    numeric_number = (
                        self._matched_question_number_int(question_match)
                        if (question_match.group("ordinal") or question_match.group("plain"))
                        else None
                    )
                    if numeric_number is not None:
                        current_question_numbers.append(numeric_number)
                continue

            explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
            if explanation_match:
                answer_mode = "explanation"
                answer_lines.append(stripped)
                continue

            if QUESTION_CATEGORY_HEADING_PATTERN.match(stripped):
                flush()
                continue

            if question_match and self._line_starts_new_question_group(
                stripped,
                answer_mode=answer_mode,
                current_max_question_number=max(current_question_numbers) if current_question_numbers else None,
            ):
                flush()
                question_lines.append(stripped)
                question_count = 1
                numeric_number = (
                    self._matched_question_number_int(question_match)
                    if (question_match.group("ordinal") or question_match.group("plain"))
                    else None
                )
                if numeric_number is not None:
                    current_question_numbers.append(numeric_number)
                continue

            answer_lines.append(stripped)

        flush()
        return groups

    def _line_starts_new_question_group(
        self,
        line: str,
        *,
        answer_mode: str,
        current_max_question_number: int | None = None,
    ) -> bool:
        matched = QUESTION_START_PATTERN.match(str(line or "").strip())
        if not matched:
            return False
        numeric_number = self._matched_question_number_int(matched)
        if (
            numeric_number is not None
            and current_max_question_number is not None
            and answer_mode in {"answer", "explanation"}
            and numeric_number <= current_max_question_number
        ):
            return False
        body = str(matched.group("body") or "").strip()
        if matched.group("wrapped") and answer_mode in {"answer", "explanation"}:
            return False
        if self._looks_like_answer_only_text(body):
            return False
        if re.match(r"^[A-HＡ-Ｈ]\s*[.．、]", body):
            return False
        if answer_mode == "explanation":
            return self._looks_like_question_prompt_text(body)
        return True

    def _looks_like_question_prompt_text(self, text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        if re.match(r"^[A-HＡ-Ｈ]\s*[.．、]", stripped):
            return False
        if re.search(r"第\s*\d+\s*题", stripped):
            return True
        return any(
            token in stripped
            for token in ("（", "( ", "（　", "求", "下列", "正确", "错误", "如图", "已知", "关于", "判断", "的是", "说明", "题")
        )

    def _matched_question_number_int(self, matched: re.Match[str] | None) -> int | None:
        if matched is None:
            return None
        raw = self._question_number_from_match(matched)
        return int(raw) if str(raw).isdigit() else None

    def _should_group_question_blocks(
        self,
        question_blocks: list[tuple[str, str]],
        answer_lookup: dict[str, dict[str, str]],
        answer_bank: str | None,
    ) -> bool:
        if len(question_blocks) < 3:
            return False
        if not str(answer_bank or "").strip():
            return False
        answered_numbers = {
            number
            for number in (answer_lookup or {})
            if answer_lookup[number].get("answer_text") or answer_lookup[number].get("explanation_text")
        }
        if len(answered_numbers) < 2:
            return False
        question_numbers = {number for number, _ in question_blocks}
        return answered_numbers.issubset(question_numbers)

    def _build_grouped_question_bank_chunk(
        self,
        document: KnowledgeDocument,
        *,
        question_text: str,
        answer_bank: str,
        question_blocks: list[tuple[str, str]],
        asset_map: dict[str, ExtractedAsset],
        source_format: str | None,
        parser_backend: str | None,
        parser_provenance: dict[str, Any] | None,
    ) -> PreparedChunk | None:
        start_number = question_blocks[0][0]
        end_number = question_blocks[-1][0]
        grouped_number = start_number if start_number == end_number else f"{start_number}-{end_number}"
        grouped_answer, grouped_explanation = self._split_grouped_answer_bank_sections(answer_bank)

        clean_question_text, question_assets = self._finalize_chunk_text_and_assets(question_text, asset_map)
        clean_answer_text, answer_assets = self._finalize_chunk_text_and_assets(grouped_answer or "", asset_map)
        clean_explanation_text, explanation_assets = self._finalize_chunk_text_and_assets(grouped_explanation or "", asset_map)
        asset_refs = question_assets or answer_assets or explanation_assets
        readable_question_text = self._normalize_question_readability_layout(clean_question_text or question_text)
        finalized_text = self._compose_question_chunk_text(
            number=grouped_number,
            question_text=readable_question_text,
            answer_text=clean_answer_text or None,
            explanation_text=clean_explanation_text or None,
        )
        if not finalized_text.strip():
            return None
        return self._build_question_bank_chunk(
            document,
            content=finalized_text,
            question_number=grouped_number,
            question_text=readable_question_text,
            answer_text=clean_answer_text or None,
            explanation_text=clean_explanation_text or None,
            asset_refs=asset_refs,
            source_format=source_format,
            source_locator=f"question-group:{grouped_number}",
            parser_backend=parser_backend,
            parser_provenance=parser_provenance,
            raw_block_text=f"{question_text}\n{answer_bank}".strip(),
        )

    def _split_grouped_answer_bank_sections(self, text: str) -> tuple[str | None, str | None]:
        answer_lines: list[str] = []
        explanation_lines: list[str] = []
        current_section = "answer"
        for line in str(text or "").split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            answer_match = ANSWER_LINE_PATTERN.match(stripped)
            explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
            if DIFFICULTY_LINE_PATTERN.match(stripped) or KNOWLEDGE_POINTS_LINE_PATTERN.match(stripped):
                continue
            if answer_match:
                current_section = "answer"
                inline_body = str(answer_match.group("body") or "").strip()
                if inline_body:
                    answer_lines.append(inline_body)
                continue
            if explanation_match:
                current_section = "explanation"
                inline_body = str(explanation_match.group("body") or "").strip()
                if inline_body:
                    explanation_lines.append(inline_body)
                continue
            if current_section == "answer":
                answer_lines.append(stripped)
            else:
                explanation_lines.append(stripped)
        answer_text = "\n".join(answer_lines).strip() or None
        explanation_text = "\n".join(explanation_lines).strip() or None
        if answer_text and explanation_text is None:
            marker_match = re.search(r"(?:【)?(?:解析|详解|解答|思路(?:点拨)?|点拨|说明|分析|点睛)(?:】)?\s*[:：]", answer_text)
            if marker_match:
                explanation_text = answer_text[marker_match.start():].strip() or None
                answer_text = answer_text[:marker_match.start()].strip() or None
                explanation_match = EXPLANATION_LINE_PATTERN.match(explanation_text)
                if explanation_match:
                    explanation_text = str(explanation_match.group("body") or "").strip() or explanation_text
        return self._split_inline_explanation_segment(answer_text, explanation_text)

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
        normalized = re.sub(r"\n(?=[（(]\d{1,2}[)）])", "\n\n", normalized)
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
                body = (matched.group("body") or "").strip()
                if matched.group("plain") and re.fullmatch(r"\d+(?:\s*[.．、]\s*\d+)*", body):
                    if current_number:
                        current_lines.append(stripped)
                    continue
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

    def _parse_answer_bank(
        self,
        text: str,
        *,
        expected_numbers: list[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        normalized_bank = self._normalize_answer_bank_text(text)
        answer_blocks = self._parse_numbered_blocks(normalized_bank)
        answer_lookup: dict[str, dict[str, str]] = {}
        for number, block_text in answer_blocks:
            answer_text, explanation_text = self._split_answer_block_sections(block_text)
            existing = answer_lookup.get(number, {})
            answer_lookup[number] = {
                "answer_text": answer_text or existing.get("answer_text") or "",
                "explanation_text": explanation_text or existing.get("explanation_text") or "",
            }
        if expected_numbers:
            targeted_lookup = self._parse_answer_bank_by_expected_numbers(text, expected_numbers)
            for number, payload in targeted_lookup.items():
                existing = answer_lookup.get(number, {})
                answer_lookup[number] = {
                    "answer_text": payload.get("answer_text") or existing.get("answer_text") or "",
                    "explanation_text": payload.get("explanation_text") or existing.get("explanation_text") or "",
                }
            answer_lookup = {
                number: answer_lookup.get(number, {"answer_text": "", "explanation_text": ""})
                for number in expected_numbers
            }
        return answer_lookup

    def _parse_answer_bank_by_expected_numbers(
        self,
        text: str,
        expected_numbers: list[str],
    ) -> dict[str, dict[str, str]]:
        expected = [str(number).strip() for number in expected_numbers if str(number).strip()]
        if not expected:
            return {}

        answer_section, explanation_section = self._split_raw_answer_bank_sections(text)
        answer_entries = self._split_section_by_expected_numbers(answer_section, expected)
        explanation_entries = self._split_section_by_expected_numbers(explanation_section, expected)

        return {
            number: {
                "answer_text": answer_entries.get(number, ""),
                "explanation_text": explanation_entries.get(number, ""),
            }
            for number in expected
        }

    def _split_raw_answer_bank_sections(self, text: str) -> tuple[str, str]:
        answer_lines: list[str] = []
        explanation_lines: list[str] = []
        current_section = "answer"
        for raw_line in str(text or "").split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue
            answer_match = ANSWER_LINE_PATTERN.match(stripped)
            explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
            if DIFFICULTY_LINE_PATTERN.match(stripped) or KNOWLEDGE_POINTS_LINE_PATTERN.match(stripped):
                continue
            if answer_match:
                current_section = "answer"
                body = str(answer_match.group("body") or "").strip()
                if body:
                    answer_lines.append(body)
                continue
            if explanation_match:
                current_section = "explanation"
                body = str(explanation_match.group("body") or "").strip()
                if body:
                    explanation_lines.append(body)
                continue
            if current_section == "answer":
                answer_lines.append(stripped)
            else:
                explanation_lines.append(stripped)
        return "\n".join(answer_lines).strip(), "\n".join(explanation_lines).strip()

    def _split_section_by_expected_numbers(
        self,
        text: str,
        expected_numbers: list[str],
    ) -> dict[str, str]:
        body = str(text or "").strip()
        if not body:
            return {}

        escaped = "|".join(sorted({re.escape(number) for number in expected_numbers}, key=len, reverse=True))
        marker_pattern = re.compile(
            rf"(?:(?P<plain>{escaped})\s*[.．、)]|(?:(?<=^)|(?<=[\s；;。]))第\s*(?P<ordinal>{escaped})\s*题)"
        )
        matches = list(marker_pattern.finditer(body))
        if not matches:
            return {}

        result: dict[str, str] = {}
        for index, match in enumerate(matches):
            number = match.group("ordinal") or match.group("plain") or ""
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            segment = body[start:end].strip()
            if segment:
                result[number] = segment
        return result

    def _normalize_answer_bank_text(self, text: str) -> str:
        normalized_lines: list[str] = []
        current_section = "answer"

        for raw_line in str(text or "").split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue
            answer_match = ANSWER_LINE_PATTERN.match(stripped)
            explanation_match = EXPLANATION_LINE_PATTERN.match(stripped)
            if answer_match:
                current_section = "answer"
                body = str(answer_match.group("body") or "").strip()
                if not body:
                    continue
                entries = self._split_inline_top_level_numbered_entries(body)
                if not entries and QUESTION_START_PATTERN.match(body):
                    entries = [body]
                normalized_lines.extend(entry.strip() for entry in (entries or [body]) if entry.strip())
                continue
            elif explanation_match:
                current_section = "explanation"
                body = str(explanation_match.group("body") or "").strip()
                if not body:
                    continue
                entries = self._split_inline_top_level_numbered_entries(body)
                if not entries and QUESTION_START_PATTERN.match(body):
                    entries = [body]
                if entries:
                    for entry in entries:
                        numbered_match = QUESTION_START_PATTERN.match(entry)
                        if not numbered_match:
                            normalized_lines.append(f"解析：{entry}")
                            continue
                        number = self._question_number_from_match(numbered_match)
                        numbered_body = str(numbered_match.group("body") or "").strip()
                        nested_answer_match = ANSWER_LINE_PATTERN.match(numbered_body)
                        if nested_answer_match:
                            answer_body = str(nested_answer_match.group("body") or "").strip()
                            normalized_lines.append(f"{number}. {answer_body}" if answer_body else f"{number}.")
                            continue
                        normalized_lines.append(f"{number}. 解析：{numbered_body}" if numbered_body else f"{number}. 解析：")
                else:
                    normalized_lines.append(f"解析：{body}")
                continue

            entries = self._split_inline_top_level_numbered_entries(stripped)
            if not entries and QUESTION_START_PATTERN.match(stripped):
                entries = [stripped]
            if entries:
                if current_section == "explanation":
                    for entry in entries:
                        numbered_match = QUESTION_START_PATTERN.match(entry)
                        if not numbered_match:
                            normalized_lines.append(f"解析：{entry}")
                            continue
                        number = self._question_number_from_match(numbered_match)
                        numbered_body = str(numbered_match.group("body") or "").strip()
                        nested_answer_match = ANSWER_LINE_PATTERN.match(numbered_body)
                        if nested_answer_match:
                            answer_body = str(nested_answer_match.group("body") or "").strip()
                            normalized_lines.append(f"{number}. {answer_body}" if answer_body else f"{number}.")
                            continue
                        normalized_lines.append(f"{number}. 解析：{numbered_body}" if numbered_body else f"{number}. 解析：")
                else:
                    normalized_lines.extend(entry.strip() for entry in entries if entry.strip())
                continue

            if current_section == "explanation" and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1]}\n{stripped}".strip()
                continue
            if current_section == "answer" and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1]}\n{stripped}".strip()
                continue

            normalized_lines.append(stripped)

        return "\n".join(normalized_lines)

    def _split_inline_top_level_numbered_entries(self, text: str) -> list[str]:
        pattern = re.compile(r"(?:(?<=^)|(?<=\s))(?=(?:第\s*\d+\s*题|\d{1,3}\s*[.．、)]))")
        positions = [match.start() for match in pattern.finditer(text)]
        if len(positions) <= 1:
            return []
        positions.append(len(text))
        entries: list[str] = []
        for start, end in zip(positions, positions[1:]):
            entry = text[start:end].strip()
            if entry:
                entries.append(entry)
        return entries

    def _merge_repeated_answer_bank_blocks(
        self,
        question_blocks: list[tuple[str, str]],
    ) -> tuple[list[tuple[str, str]], dict[str, dict[str, str]]]:
        if len(question_blocks) < 3:
            return question_blocks, {}

        split_index = self._find_repeated_answer_bank_split(question_blocks)
        if split_index is None:
            return question_blocks, {}

        main_blocks = question_blocks[:split_index]
        answer_blocks = question_blocks[split_index:]
        answer_lookup: dict[str, dict[str, str]] = {}
        for number, block_text in answer_blocks:
            answer_text, explanation_text = self._extract_repeated_answer_payload(block_text)
            if not answer_text and not explanation_text:
                return question_blocks, {}
            answer_lookup[number] = {
                "answer_text": answer_text or "",
                "explanation_text": explanation_text or "",
            }
        return main_blocks, answer_lookup

    def _find_repeated_answer_bank_split(
        self,
        question_blocks: list[tuple[str, str]],
    ) -> int | None:
        if len(question_blocks) < 3:
            return None

        for split_index in range(1, len(question_blocks)):
            prefix = question_blocks[:split_index]
            suffix = question_blocks[split_index:]
            if len(suffix) < 2:
                continue
            prefix_order: list[str] = []
            seen_numbers: set[str] = set()
            for number, _ in prefix:
                if number not in seen_numbers:
                    seen_numbers.add(number)
                    prefix_order.append(number)
            suffix_numbers = [number for number, _ in suffix]
            if not suffix_numbers:
                continue
            if len(set(suffix_numbers)) != len(suffix_numbers):
                continue
            if any(number not in seen_numbers for number in suffix_numbers):
                continue
            ordered_suffix = sorted(suffix_numbers, key=prefix_order.index)
            if suffix_numbers != ordered_suffix:
                continue
            answer_like_count = sum(
                1 for _, block_text in suffix if self._looks_like_repeated_answer_block(block_text)
            )
            if answer_like_count >= max(1, len(suffix) - 1):
                return split_index
        return None

    def _looks_like_repeated_answer_block(self, block_text: str) -> bool:
        answer_text, explanation_text = self._extract_repeated_answer_payload(block_text)
        return bool(answer_text or explanation_text)

    def _extract_repeated_answer_payload(
        self,
        block_text: str,
    ) -> tuple[str | None, str | None]:
        question_text, answer_text, explanation_text = self._split_question_block_sections(block_text)
        clean_question = str(question_text or "").strip()
        clean_answer = str(answer_text or "").strip() or None
        clean_explanation = str(explanation_text or "").strip() or None

        clean_answer, clean_explanation = self._split_inline_explanation_segment(
            clean_answer,
            clean_explanation,
        )
        clean_question, clean_explanation = self._split_inline_explanation_segment(
            clean_question,
            clean_explanation,
        )

        if clean_answer or clean_explanation:
            if not clean_answer and clean_question and self._looks_like_answer_only_text(clean_question):
                clean_answer = clean_question
            elif clean_question and not self._looks_like_answer_only_text(clean_question):
                return None, None
            return clean_answer, clean_explanation

        if self._looks_like_answer_only_text(clean_question):
            return clean_question, None
        return None, None

    def _split_inline_explanation_segment(
        self,
        answer_text: str | None,
        explanation_text: str | None,
    ) -> tuple[str | None, str | None]:
        clean_answer = str(answer_text or "").strip() or None
        clean_explanation = str(explanation_text or "").strip() or None
        if not clean_answer or clean_explanation:
            return clean_answer, clean_explanation
        matched = INLINE_EXPLANATION_SEGMENT_PATTERN.match(clean_answer)
        if not matched:
            return clean_answer, clean_explanation
        answer_part = str(matched.group("answer") or "").strip() or None
        explanation_part = str(matched.group("explanation") or "").strip() or None
        if explanation_part:
            explanation_match = EXPLANATION_LINE_PATTERN.match(explanation_part)
            if explanation_match:
                explanation_part = str(explanation_match.group("body") or "").strip() or explanation_part
        return answer_part, explanation_part

    def _looks_like_answer_only_text(self, text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        if re.search(r"第\s*\d+\s*题", stripped) or "题" in stripped:
            return False
        if len(stripped) <= 24 and ANSWER_ONLY_TOKEN_PATTERN.match(stripped):
            return True
        if "\n" in stripped:
            lines = [line.strip() for line in stripped.split("\n") if line.strip()]
            return bool(lines) and all(self._looks_like_answer_only_text(line) for line in lines)
        if any(token in stripped for token in ("答案", "解析", "详解")):
            return True
        if len(stripped) <= 80 and not any(
            token in stripped for token in ("如图", "求", "已知", "下列", "判断", "计算", "实验", "分析", "选择")
        ):
            return True
        return False

    def _looks_like_exam_cover_block(self, text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        return bool(re.search(r"学校[:：].*姓名[:：].*班级[:：]", stripped))

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
