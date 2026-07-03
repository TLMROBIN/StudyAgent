"""Shared chunk data models, moved verbatim from backend.services.rag_service."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.services.pdf_parse_types import ExtractedAsset, PDFParseResult


@dataclass
class ExtractionResult:
    text: str
    assets: list[ExtractedAsset] = field(default_factory=list)
    parsed_pdf: PDFParseResult | None = None
    parser_backend: str | None = None
    parser_provenance: dict[str, Any] | None = None
    source_format: str | None = None


@dataclass
class PreparedChunk:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
