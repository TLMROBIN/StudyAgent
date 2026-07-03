"""txt/markdown chunk building: markdown normalization and formula-aware splitting.

Moved verbatim from backend.services.rag_service.RagService.
"""
from __future__ import annotations

import re


PLAIN_TEXT_SPLIT_PATTERN = re.compile(r"(?<=[。！？；;.!?])\s+|\n+")
CHUNK_BOUNDARY_HINTS = "。！？；;.!?，,"
MARKDOWN_BLOCK_START_PATTERN = re.compile(
    r"^(?:#{1,6}\s|[*+-]\s|[0-9]{1,2}(?:\\?\.)\s|[0-9]{1,2}[)）]\s|>\s|```|~~~|\|)"
)
INLINE_MATH_TERMINAL_PATTERN = re.compile(r"(?:\$(?!\$)[^$]+\$|\\\([^)]*\\\))$")
PUNCTUATION_ONLY_PATTERN = re.compile(r"^[，。！？；：、,.;!?）)】》]+$")


class TextChunkBuilderMixin:
    """Mixin providing txt/md normalization and formula-aware text splitting for RagService."""

    def split_text(self, text: str) -> list[str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []
        return self._split_formula_aware_text(text)

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
