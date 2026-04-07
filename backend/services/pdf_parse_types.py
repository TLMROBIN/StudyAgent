from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedAsset:
    asset_id: str
    filename: str
    content_type: str
    storage_path: str
    public_url: str
    title: str | None = None
    description: str | None = None


@dataclass
class PDFBlock:
    page_index: int
    block_type: str
    text: str = ""
    level: int | None = None
    asset_id: str | None = None
    hierarchy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PDFParseResult:
    text: str
    blocks: list[PDFBlock] = field(default_factory=list)
    assets: list[ExtractedAsset] = field(default_factory=list)
    parser_backend: str = "mineru"
    parser_provenance: dict[str, Any] = field(default_factory=dict)
