"""Shared MinerU content_list → PDFBlock normalization helpers.

These are pure transformations extracted from ``mineru_service.py`` so that both
the local subprocess backend (``mineru_service``) and the remote HTTP API backend
(``mineru_remote_service``) normalize MinerU ``content_list`` JSON identically.
Behavior must stay byte-for-byte compatible with the original implementations.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
import re
import shutil
from typing import Any

from backend.services.pdf_parse_types import ExtractedAsset, PDFBlock


def strip_office_text_style_artifacts(text: str) -> str:
    normalized = str(text or "")
    if "<text" not in normalized.lower() and "</text>" not in normalized.lower() and 'style="' not in normalized.lower():
        return normalized.strip()
    normalized = re.sub(r'(?i)<+\s*/?\s*text(?:\s+style="[^"]*")?\s*>', "", normalized)
    normalized = re.sub(r"(?i)</\s*text\s*>", "", normalized)
    normalized = re.sub(r'(?i)(?:text|ext|xt)\s+style="[^"]*">', "", normalized)
    normalized = re.sub(r"(?i)<[^>\n]*text[^>\n]*>", "", normalized)
    normalized = normalized.replace("<", "").replace(">", "")
    return normalized.strip()


def looks_like_inline_content_list(value: list[Any]) -> bool:
    saw_inline = False
    for item in value:
        if isinstance(item, str):
            if item.strip():
                saw_inline = True
            continue
        if not isinstance(item, dict):
            return False
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {
            "text",
            "equation_inline",
            "inline_equation",
            "equation_interline",
            "interline_equation",
        } or item.get("math_content"):
            saw_inline = True
            continue
        return False
    return saw_inline


def flatten_inline_content(value: list[Any]) -> str:
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item:
                parts.append(item)
            continue
        if not isinstance(item, dict):
            rendered = flatten_content(item)
            if rendered:
                parts.append(rendered)
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "text":
            raw_text = str(item.get("content") or "")
            if raw_text:
                parts.append(raw_text)
            continue
        if item_type in {"equation_inline", "inline_equation", "equation_interline", "interline_equation"} or item.get("math_content"):
            rendered = flatten_content(item)
            if rendered:
                parts.append(f"\n{rendered}\n")
            continue
        rendered = flatten_content(item)
        if rendered:
            parts.append(rendered)
    normalized = strip_office_text_style_artifacts("".join(parts))
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def flatten_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return strip_office_text_style_artifacts(value.strip())
    if isinstance(value, list):
        if looks_like_inline_content_list(value):
            return flatten_inline_content(value)
        parts = [flatten_content(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        if value.get("type") == "text":
            return strip_office_text_style_artifacts(str(value.get("content") or "").strip())
        item_type = str(value.get("type") or "").strip().lower()
        if item_type in {"equation_inline", "inline_equation"}:
            payload = str(value.get("content") or "").strip()
            return f"equation_inline\n{payload}".strip() if payload else "equation_inline"
        if item_type in {"equation_interline", "interline_equation"}:
            payload = str(value.get("content") or "").strip()
            return f"equation_display\n{payload}".strip() if payload else "equation_display"
        if value.get("math_content"):
            payload = str(value.get("math_content") or "").strip()
            return f"equation_display\n{payload}".strip() if payload else ""
        if value.get("html"):
            return str(value.get("html") or "").strip()
        ordered_keys = [
            "title_content",
            "paragraph_content",
            "table_caption",
            "table_body",
            "table_footnote",
            "image_caption",
            "image_footnote",
            "algorithm_caption",
            "algorithm_content",
            "algorithm_footnote",
            "chart_caption",
            "chart_footnote",
            "list_items",
            "page_header_content",
            "page_footer_content",
            "page_number_content",
        ]
        parts = [flatten_content(value.get(key)) for key in ordered_keys if key in value]
        if not any(parts):
            parts = [flatten_content(item) for item in value.values()]
        return "\n".join(part for part in parts if part).strip()
    return str(value).strip()


def content_roles(content: Any) -> list[str]:
    if not isinstance(content, dict):
        return []
    ordered_keys = [
        "title_content",
        "paragraph_content",
        "table_caption",
        "table_body",
        "table_footnote",
        "image_caption",
        "image_footnote",
        "algorithm_caption",
        "algorithm_content",
        "algorithm_footnote",
        "chart_caption",
        "chart_footnote",
        "list_items",
        "page_header_content",
        "page_footer_content",
        "page_number_content",
    ]
    roles: list[str] = []
    for key in ordered_keys:
        if key not in content:
            continue
        flattened = flatten_content(content.get(key))
        if flattened:
            roles.append(key)
    return roles


def extract_image_path(item: dict[str, Any]) -> str | None:
    content = item.get("content") or {}
    image_source = content.get("image_source") if isinstance(content, dict) else None
    if isinstance(image_source, dict):
        path = image_source.get("path")
        if path:
            return str(path)
    return None


def normalize_block(
    item: dict[str, Any],
    page_index: int,
    asset_lookup: dict[str, ExtractedAsset],
) -> PDFBlock | None:
    if not isinstance(item, dict):
        return None
    block_type = str(item.get("type") or "paragraph")
    content = item.get("content") or {}
    text = flatten_content(content)
    asset_id = None
    image_path = extract_image_path(item)
    if image_path and image_path in asset_lookup:
        asset = asset_lookup[image_path]
        asset_id = asset.asset_id
        marker = f"[[asset:{asset.asset_id}]]"
        text = f"{marker}\n{text}".strip() if text else marker
    level = None
    if isinstance(content, dict):
        title_content = content.get("title_content") or {}
        if isinstance(title_content, dict):
            level = title_content.get("level")
        level = level or content.get("level")
    roles = content_roles(content)
    return PDFBlock(
        page_index=page_index,
        block_type=block_type,
        text=text.strip(),
        level=int(level) if isinstance(level, int) else None,
        asset_id=asset_id,
        metadata={
            "raw_type": block_type,
            "content_roles": roles,
            "table_type": content.get("table_type") if isinstance(content, dict) else None,
            "table_nest_level": content.get("table_nest_level") if isinstance(content, dict) else None,
        },
    )


def assemble_content_list_result(
    data: list[Any],
    *,
    content_list_dir: Path,
    document_asset_dir: Path,
    document_id: int,
) -> tuple[str, list[PDFBlock], list[ExtractedAsset]]:
    """Copy referenced images into the document asset dir and normalize blocks.

    Mirrors the asset assembly + block normalization used by the local MinerU
    backend (``MineruService._parse_content_list_document``), shared with the
    remote API backend. ``content_list_dir`` is the directory that relative
    image paths inside the content_list resolve against.
    """
    document_asset_dir.mkdir(parents=True, exist_ok=True)

    image_paths = sorted({extract_image_path(item) for page in data for item in page if extract_image_path(item)})
    asset_lookup: dict[str, ExtractedAsset] = {}
    assets: list[ExtractedAsset] = []
    for index, relative_path in enumerate(image_paths, start=1):
        source = content_list_dir / relative_path
        if not source.is_file():
            continue
        suffix = source.suffix.lower() or ".bin"
        asset_id = f"image-{index:03d}"
        filename = f"{asset_id}{suffix}"
        target = document_asset_dir / filename
        shutil.copy2(source, target)
        asset = ExtractedAsset(
            asset_id=asset_id,
            filename=filename,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            storage_path=str(target),
            public_url=f"/api/knowledge/documents/{document_id}/assets/{filename}",
            title=source.stem,
            description=relative_path,
        )
        asset_lookup[relative_path] = asset
        assets.append(asset)

    blocks: list[PDFBlock] = []
    text_parts: list[str] = []
    for page_index, page in enumerate(data):
        if not isinstance(page, list):
            raise ValueError("Each MinerU page entry must be a list")
        for item in page:
            block = normalize_block(item, page_index, asset_lookup)
            if block is None:
                continue
            blocks.append(block)
            if block.text.strip():
                text_parts.append(block.text.strip())

    return "\n\n".join(text_parts).strip(), blocks, assets
