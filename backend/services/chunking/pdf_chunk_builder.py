"""Legacy PDF text extraction: multi-extractor candidates, page scoring, normalization.

Moved verbatim from backend.services.rag_service.RagService.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass
class PDFExtractionCandidate:
    extractor: str
    pages: list[str]


class PdfChunkBuilderMixin:
    """Mixin providing legacy (non-MinerU) PDF text extraction for RagService."""

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
