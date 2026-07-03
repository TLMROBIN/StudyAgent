"""DOCX content extraction: OOXML parsing, OMML->LaTeX, legacy OLE equations, image assets.

Moved verbatim from backend.services.rag_service.RagService.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath
import posixpath
import re
import struct
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

from backend.services.chunking.models import ExtractionResult
from backend.services.pdf_parse_types import ExtractedAsset


LEGACY_QUESTION_DOCX_FORMULA_MESSAGE = (
    "检测到 MathType 类 legacy 公式，当前不支持；请改用微软公式（OMML）后重新导入"
)
LEGACY_DOCX_OLE_PROG_IDS = {
    "Equation.DSMT4",
    "MathType 6.0 Equation",
    "MathType 7.0 Equation",
    "MathType EF",
}
DOCX_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
DOCX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCX_OLE_NS = "urn:schemas-microsoft-com:office:office"
DOCX_VML_NS = "urn:schemas-microsoft-com:vml"
OLE_END_OF_CHAIN = 0xFFFFFFFE
OLE_FREE_SECTOR = 0xFFFFFFFF
OLE_UINT32_SIZE = 4
OLE_CF_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OLE_METADATA_STRINGS = {
    "MathType 6.0 Equation",
    "MathType 7.0 Equation",
    "MathType EF",
    "Equation.DSMT4",
    "DSMT6",
    "DSMT7",
    "WinAllBasicCodePages",
    "WinAllCodePages",
    "Times New Roman",
    "Symbol",
    "Courier New",
    "MT Extra",
    "Root Entry",
    "Ole",
    "CompObj",
    "ObjInfo",
    "Equation Native",
    "OlePres000",
    "AppsMFCC",
    "Design Science, Inc.",
    "System",
}
OMML_OPERATOR_MAP = {
    "∑": r"\sum",
    "∏": r"\prod",
    "∐": r"\coprod",
    "∫": r"\int",
    "∮": r"\oint",
    "⋂": r"\bigcap",
    "⋃": r"\bigcup",
}
OMML_DELIMITER_MAP = {
    "{": r"\{",
    "}": r"\}",
    "⟨": r"\langle",
    "⟩": r"\rangle",
    "⌊": r"\lfloor",
    "⌋": r"\rfloor",
    "⌈": r"\lceil",
    "⌉": r"\rceil",
}
OMML_ACCENT_MAP = {
    "̂": r"\hat",
    "^": r"\hat",
    "̄": r"\bar",
    "¯": r"\bar",
    "→": r"\vec",
    "⃗": r"\vec",
    "˙": r"\dot",
    "¨": r"\ddot",
    "̃": r"\tilde",
}


class DocxChunkBuilderMixin:
    """Mixin providing DOCX text/asset/formula extraction for RagService."""

    def _docx_contains_legacy_formula_objects(self, file_path: str) -> bool:
        try:
            with zipfile.ZipFile(file_path) as archive:
                for name in archive.namelist():
                    normalized = str(name or "")
                    if not normalized.startswith("word/") or not normalized.endswith(".xml"):
                        continue
                    try:
                        root = ET.fromstring(archive.read(normalized))
                    except (KeyError, ET.ParseError):
                        continue
                    for element in root.iter():
                        if (
                            self._xml_namespace(element.tag) == DOCX_OLE_NS
                            and self._xml_local_name(element.tag) == "OLEObject"
                            and str(element.attrib.get("ProgID") or "").strip() in LEGACY_DOCX_OLE_PROG_IDS
                        ):
                            return True
        except (KeyError, zipfile.BadZipFile):
            return False
        return False

    def _extract_docx_text(self, file_path: str) -> str:
        return self._extract_docx_content(file_path).text

    def _extract_docx_content(self, file_path: str, *, document_id: int | None = None) -> ExtractionResult:
        try:
            with zipfile.ZipFile(file_path) as archive:
                document_xml = archive.read("word/document.xml")
                relationships = self._load_docx_relationships(archive)
                asset_dir: Path | None = None
                if document_id is not None:
                    self.clear_document_artifacts(document_id)
                    asset_dir = self.document_asset_dir(document_id)
                    asset_dir.mkdir(parents=True, exist_ok=True)
                context: dict[str, Any] = {
                    "archive": archive,
                    "relationships": relationships,
                    "asset_dir": asset_dir,
                    "assets": [],
                    "asset_cache": {},
                    "document_id": document_id,
                }
                return self._parse_docx_document(document_xml, context)
        except KeyError as exc:
            raise RuntimeError("DOCX 文件缺少 word/document.xml，无法解析正文内容") from exc
        except zipfile.BadZipFile as exc:
            raise RuntimeError("DOCX 文件损坏或格式不正确") from exc

    def _parse_docx_document(self, document_xml: bytes, context: dict[str, Any]) -> ExtractionResult:
        try:
            root = ET.fromstring(document_xml)
        except ET.ParseError as exc:
            raise RuntimeError("DOCX 文档 XML 解析失败") from exc

        body = root.find(f".//{{{DOCX_WORD_NS}}}body")
        if body is None:
            return ExtractionResult(text="")

        blocks: list[str] = []
        for child in body:
            local_name = self._xml_local_name(child.tag)
            if local_name == "p":
                text = self._docx_paragraph_text(child, context)
            elif local_name == "tbl":
                text = self._docx_table_text(child, context)
            else:
                text = self._docx_node_text(child, context)
            normalized = self._normalize_docx_block_text(text)
            if normalized:
                blocks.append(normalized)
        return ExtractionResult(text="\n".join(blocks).strip(), assets=list(context["assets"]))

    def _load_docx_relationships(self, archive: zipfile.ZipFile) -> dict[str, str]:
        try:
            rels_xml = archive.read("word/_rels/document.xml.rels")
        except KeyError:
            return {}
        try:
            root = ET.fromstring(rels_xml)
        except ET.ParseError:
            return {}
        relationships: dict[str, str] = {}
        for child in root:
            if self._xml_local_name(child.tag) != "Relationship":
                continue
            relation_id = child.attrib.get("Id")
            target = child.attrib.get("Target")
            if not relation_id or not target:
                continue
            relationships[relation_id] = self._resolve_docx_target_path("word/document.xml", target)
        return relationships

    def _resolve_docx_target_path(self, source_path: str, target: str) -> str:
        base_dir = PurePosixPath(source_path).parent
        target_path = PurePosixPath(target.lstrip("/")) if target.startswith("/") else (base_dir / PurePosixPath(target))
        return posixpath.normpath(str(target_path))

    def _docx_paragraph_text(self, paragraph: ET.Element, context: dict[str, Any]) -> str:
        return self._normalize_docx_block_text("".join(self._docx_node_text(child, context) for child in paragraph))

    def _docx_table_text(self, table: ET.Element, context: dict[str, Any]) -> str:
        rows: list[str] = []
        for row in table:
            if self._xml_local_name(row.tag) != "tr":
                continue
            cells: list[str] = []
            for cell in row:
                if self._xml_local_name(cell.tag) != "tc":
                    continue
                cell_blocks: list[str] = []
                for child in cell:
                    local_name = self._xml_local_name(child.tag)
                    if local_name == "p":
                        block_text = self._docx_paragraph_text(child, context)
                    elif local_name == "tbl":
                        block_text = self._docx_table_text(child, context)
                    else:
                        block_text = self._docx_node_text(child, context)
                    normalized = self._normalize_docx_block_text(block_text)
                    if normalized:
                        cell_blocks.append(normalized)
                cell_text = "\n".join(cell_blocks).strip()
                if cell_text:
                    cells.append(cell_text)
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows).strip()

    def _docx_node_text(self, element: ET.Element, context: dict[str, Any]) -> str:
        local_name = self._xml_local_name(element.tag)
        namespace = self._xml_namespace(element.tag)
        if namespace == DOCX_MATH_NS and local_name in {"oMath", "oMathPara"}:
            return self._omml_to_latex(element)
        if namespace == DOCX_WORD_NS and local_name == "object":
            return self._docx_ole_object_text(element, context)
        if namespace == DOCX_WORD_NS and local_name in {"drawing", "pict"}:
            return self._docx_image_marker(element, context)

        if namespace in {DOCX_WORD_NS, DOCX_MATH_NS} and local_name in {"t", "delText", "instrText"}:
            return element.text or ""
        if namespace == DOCX_WORD_NS and local_name == "tab":
            return "\t"
        if namespace == DOCX_WORD_NS and local_name in {"br", "cr"}:
            return "\n"

        parts: list[str] = []
        if element.text and element.text.strip():
            parts.append(element.text)
        for child in element:
            parts.append(self._docx_node_text(child, context))
            if child.tail and child.tail.strip():
                parts.append(child.tail)
        return "".join(parts)

    def _docx_ole_object_text(self, element: ET.Element, context: dict[str, Any]) -> str:
        formula_text = self._docx_extract_ole_formula_text(element, context)
        if formula_text:
            wrapped = self._wrap_formula(formula_text, display=False)
            return f" {wrapped} " if wrapped else ""
        fallback = self._docx_ole_object_fallback_label(element)
        if fallback:
            return f" {fallback} "
        return ""

    def _docx_extract_ole_formula_text(self, element: ET.Element, context: dict[str, Any]) -> str | None:
        embed_id = None
        prog_id = None
        for descendant in element.iter():
            if self._xml_namespace(descendant.tag) == DOCX_OLE_NS and self._xml_local_name(descendant.tag) == "OLEObject":
                embed_id = self._xml_attr(descendant, "id") or embed_id
                prog_id = descendant.attrib.get("ProgID") or prog_id
                break
        if not embed_id or not prog_id or prog_id != "Equation.DSMT4":
            return None
        target_path = context["relationships"].get(embed_id)
        if not target_path:
            return None
        archive: zipfile.ZipFile = context["archive"]
        try:
            payload = archive.read(target_path)
        except KeyError:
            return None
        return self._extract_legacy_equation_text(payload)

    def _docx_ole_object_fallback_label(self, element: ET.Element) -> str | None:
        for descendant in element.iter():
            if self._xml_namespace(descendant.tag) == DOCX_VML_NS and self._xml_local_name(descendant.tag) == "shape":
                alt = descendant.attrib.get("alt")
                if alt:
                    return "【公式对象】"
            if self._xml_namespace(descendant.tag) == DOCX_VML_NS and self._xml_local_name(descendant.tag) == "imagedata":
                title = self._xml_attr(descendant, "title")
                if title:
                    return "【公式对象】"
        return "【公式对象】"

    def _extract_legacy_equation_text(self, payload: bytes) -> str | None:
        stream = self._extract_ole_stream(payload, "Equation Native")
        if not stream:
            return None
        tex_text = self._extract_equation_tex_text(stream)
        if tex_text:
            return tex_text
        return self._extract_equation_char_fallback(stream)

    def _extract_ole_stream(self, payload: bytes, stream_name: str) -> bytes | None:
        if len(payload) < 512 or not payload.startswith(OLE_CF_MAGIC):
            return None
        try:
            sector_size = 1 << self._ole_u16(payload, 30)
            mini_sector_size = 1 << self._ole_u16(payload, 32)
            directory_start = self._ole_u32(payload, 48)
            mini_stream_cutoff = self._ole_u32(payload, 56)
            mini_fat_start = self._ole_u32(payload, 60)
            difat = [
                self._ole_u32(payload, 76 + index * OLE_UINT32_SIZE)
                for index in range(109)
            ]
            fat: list[int] = []
            for sector in [item for item in difat if item != OLE_FREE_SECTOR]:
                offset = 512 + sector * sector_size
                fat.extend(
                    struct.unpack(
                        "<" + "I" * (sector_size // OLE_UINT32_SIZE),
                        payload[offset : offset + sector_size],
                    )
                )
            directory_bytes = b"".join(
                payload[512 + sector * sector_size : 512 + (sector + 1) * sector_size]
                for sector in self._ole_sector_chain(fat, directory_start)
            )
            entries: dict[str, tuple[int, int, int]] = {}
            for index in range(0, len(directory_bytes), 128):
                entry = directory_bytes[index : index + 128]
                name_length = self._ole_u16(entry, 64)
                if name_length < 2:
                    continue
                name = entry[: name_length - 2].decode("utf-16le", "ignore")
                entries[name] = (
                    entry[66],
                    self._ole_u32(entry, 116),
                    struct.unpack_from("<Q", entry, 120)[0],
                )
            if "Root Entry" not in entries or stream_name not in entries:
                return None
            mini_fat: list[int] = []
            for sector in self._ole_sector_chain(fat, mini_fat_start):
                offset = 512 + sector * sector_size
                mini_fat.extend(
                    struct.unpack(
                        "<" + "I" * (sector_size // OLE_UINT32_SIZE),
                        payload[offset : offset + sector_size],
                    )
                )
            _, root_start, root_size = entries["Root Entry"]
            root_stream = b"".join(
                payload[512 + sector * sector_size : 512 + (sector + 1) * sector_size]
                for sector in self._ole_sector_chain(fat, root_start)
            )[:root_size]
            _, stream_start, stream_size = entries[stream_name]
            if stream_size < mini_stream_cutoff:
                chunks: list[bytes] = []
                sector = stream_start
                seen: set[int] = set()
                while sector not in {OLE_END_OF_CHAIN, OLE_FREE_SECTOR} and sector not in seen:
                    seen.add(sector)
                    offset = sector * mini_sector_size
                    chunks.append(root_stream[offset : offset + mini_sector_size])
                    sector = mini_fat[sector]
                return b"".join(chunks)[:stream_size]
            return b"".join(
                payload[512 + sector * sector_size : 512 + (sector + 1) * sector_size]
                for sector in self._ole_sector_chain(fat, stream_start)
            )[:stream_size]
        except (IndexError, KeyError, struct.error, ValueError):
            return None

    def _ole_sector_chain(self, fat: list[int], start_sector: int) -> list[int]:
        chain: list[int] = []
        seen: set[int] = set()
        sector = start_sector
        while sector not in {OLE_END_OF_CHAIN, OLE_FREE_SECTOR} and sector not in seen:
            if sector < 0 or sector >= len(fat):
                break
            seen.add(sector)
            chain.append(sector)
            sector = fat[sector]
        return chain

    def _ole_u32(self, buffer: bytes, offset: int) -> int:
        return struct.unpack_from("<I", buffer, offset)[0]

    def _ole_u16(self, buffer: bytes, offset: int) -> int:
        return struct.unpack_from("<H", buffer, offset)[0]

    def _extract_equation_tex_text(self, stream: bytes) -> str | None:
        decoded = stream.decode("latin1", "ignore")
        matched = re.search(r"TeX Input Language\x00([^\x00]{1,200})\x00", decoded)
        if not matched:
            return None
        return self._normalize_legacy_formula_text(matched.group(1))

    def _extract_equation_char_fallback(self, stream: bytes) -> str | None:
        printable = []
        for byte in stream:
            if 32 <= byte < 127:
                printable.append(chr(byte))
            else:
                printable.append("\n")
        lines = [line.strip() for line in re.sub(r"\n+", "\n", "".join(printable)).split("\n") if line.strip()]
        if not lines:
            return None
        filtered_lines = [line for line in lines if line not in OLE_METADATA_STRINGS]
        if not filtered_lines:
            return None
        tail: list[str] = []
        for line in reversed(filtered_lines):
            if len(line) == 1 and re.fullmatch(r"[A-Za-z0-9=+\-*/()\\]", line):
                tail.append(line)
                continue
            break
        if tail:
            return self._normalize_legacy_formula_text("".join(reversed(tail)))
        candidates = [
            line
            for line in filtered_lines
            if re.search(r"[A-Za-z0-9]", line)
            and any(token in line for token in ("\\", "=", "(", ")", "+", "-", "*", "/"))
        ]
        if candidates:
            return self._normalize_legacy_formula_text(candidates[-1])
        if len(filtered_lines[-1]) <= 16 and re.search(r"[A-Za-z0-9]", filtered_lines[-1]):
            return self._normalize_legacy_formula_text(filtered_lines[-1])
        return None

    def _normalize_legacy_formula_text(self, text: str) -> str | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        normalized = normalized.replace("\xa0", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"(?<=[A-Za-z])==(?=[A-Za-z0-9])", "=", normalized)
        normalized = normalized.replace("\\\\", "\\")
        return normalized.strip() or None

    def _docx_image_marker(self, element: ET.Element, context: dict[str, Any]) -> str:
        embed_id = self._docx_embed_id(element)
        if not embed_id:
            return ""
        asset = self._docx_extract_asset(embed_id, element, context)
        if not asset:
            return ""
        return f" [[asset:{asset.asset_id}]] "

    def _docx_embed_id(self, element: ET.Element) -> str | None:
        for descendant in element.iter():
            if self._xml_local_name(descendant.tag) == "blip":
                embed_id = self._xml_attr(descendant, "embed") or self._xml_attr(descendant, "link")
                if embed_id:
                    return embed_id
            if self._xml_local_name(descendant.tag) == "imagedata":
                embed_id = self._xml_attr(descendant, "id")
                if embed_id:
                    return embed_id
        return None

    def _docx_extract_asset(
        self,
        embed_id: str,
        element: ET.Element,
        context: dict[str, Any],
    ) -> ExtractedAsset | None:
        cached = context["asset_cache"].get(embed_id)
        if cached:
            return cached
        target_path = context["relationships"].get(embed_id)
        if not target_path:
            return None
        archive: zipfile.ZipFile = context["archive"]
        try:
            payload = archive.read(target_path)
        except KeyError:
            return None
        filename = Path(target_path).name
        suffix = Path(filename).suffix or ".bin"
        asset_id = f"image-{len(context['assets']) + 1:03d}"
        asset_filename = f"{asset_id}{suffix}"
        asset_dir: Path | None = context.get("asset_dir")
        storage_path = ""
        public_url = ""
        document_id = context.get("document_id")
        if asset_dir is not None:
            target_file = asset_dir / asset_filename
            target_file.write_bytes(payload)
            storage_path = str(target_file)
            if document_id is not None:
                public_url = f"/api/knowledge/documents/{document_id}/assets/{asset_filename}"
        metadata = self._docx_image_metadata(element)
        asset = ExtractedAsset(
            asset_id=asset_id,
            filename=asset_filename,
            content_type=mimetypes.guess_type(asset_filename)[0] or "application/octet-stream",
            storage_path=storage_path,
            public_url=public_url,
            title=metadata.get("title"),
            description=metadata.get("description"),
        )
        context["asset_cache"][embed_id] = asset
        context["assets"].append(asset)
        return asset

    def _docx_image_metadata(self, element: ET.Element) -> dict[str, str | None]:
        for descendant in element.iter():
            if self._xml_local_name(descendant.tag) != "docPr":
                continue
            title = self._xml_attr(descendant, "title") or self._xml_attr(descendant, "name")
            description = self._xml_attr(descendant, "descr")
            return {"title": title, "description": description}
        return {"title": None, "description": None}

    def _normalize_docx_block_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = text.replace("\xa0", " ")
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n[ \t]+", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _omml_to_latex(self, element: ET.Element) -> str:
        local_name = self._xml_local_name(element.tag)
        if local_name == "oMathPara":
            formulas = [
                self._wrap_formula(self._omml_children_text(child), display=True)
                for child in element
                if self._xml_local_name(child.tag) == "oMath" and self._omml_children_text(child)
            ]
            if formulas:
                return "\n".join(formulas)
        if local_name == "oMath":
            formula = self._omml_children_text(element)
            return self._wrap_formula(formula, display=False)
        return self._omml_raw_text(element)

    def _omml_children_text(self, element: ET.Element) -> str:
        parts: list[str] = []
        if element.text and element.text.strip():
            parts.append(element.text)
        for child in element:
            if self._xml_local_name(child.tag).endswith("Pr"):
                continue
            part = self._omml_raw_text(child)
            if part:
                parts.append(part)
            if child.tail and child.tail.strip():
                parts.append(child.tail)
        return "".join(parts).strip()

    def _omml_raw_text(self, element: ET.Element) -> str:
        local_name = self._xml_local_name(element.tag)
        if local_name == "t":
            return (element.text or "").strip()
        if local_name in {"oMath", "oMathPara", "r", "e", "num", "den", "sup", "sub", "deg", "fName", "groupChr"}:
            return self._omml_children_text(element)
        if local_name == "f":
            numerator = self._omml_child_text(element, "num")
            denominator = self._omml_child_text(element, "den")
            if numerator or denominator:
                return fr"\frac{{{numerator}}}{{{denominator}}}"
            return ""
        if local_name == "sSup":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sup=self._omml_child_text(element, "sup"),
            )
        if local_name == "sSub":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sub=self._omml_child_text(element, "sub"),
            )
        if local_name == "sSubSup":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sub=self._omml_child_text(element, "sub"),
                sup=self._omml_child_text(element, "sup"),
            )
        if local_name == "rad":
            body = self._omml_child_text(element, "e")
            degree = self._omml_child_text(element, "deg")
            if degree:
                return fr"\sqrt[{degree}]{{{body}}}"
            return fr"\sqrt{{{body}}}"
        if local_name == "nary":
            operator = self._omml_nary_operator(element)
            sub = self._omml_child_text(element, "sub")
            sup = self._omml_child_text(element, "sup")
            body = self._omml_child_text(element, "e")
            result = operator
            if sub:
                result += f"_{{{sub}}}"
            if sup:
                result += f"^{{{sup}}}"
            if body:
                result += f" {body}"
            return result
        if local_name == "d":
            body = self._omml_child_text(element, "e")
            begin_char, end_char = self._omml_delimiters(element)
            return fr"\left{begin_char}{body}\right{end_char}"
        if local_name == "func":
            name = self._omml_child_text(element, "fName")
            body = self._omml_child_text(element, "e")
            return f"{name}{body}"
        if local_name == "limLow":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sub=self._omml_child_text(element, "lim"),
            )
        if local_name == "limUpp":
            return self._latex_attach(
                base=self._omml_child_text(element, "e"),
                sup=self._omml_child_text(element, "lim"),
            )
        if local_name == "acc":
            body = self._omml_child_text(element, "e")
            accent = self._omml_accent_command(element)
            if accent:
                return fr"{accent}{{{body}}}"
            return body
        return self._omml_children_text(element)

    def _omml_child_text(self, element: ET.Element, child_name: str) -> str:
        child = self._omml_child(element, child_name)
        if child is None:
            return ""
        return self._omml_children_text(child)

    def _omml_child(self, element: ET.Element, child_name: str) -> ET.Element | None:
        for child in element:
            if self._xml_local_name(child.tag) == child_name:
                return child
        return None

    def _omml_nary_operator(self, element: ET.Element) -> str:
        operator = ""
        properties = self._omml_child(element, "naryPr")
        if properties is not None:
            character = self._omml_child(properties, "chr")
            if character is not None:
                operator = self._xml_attr(character, "val") or (character.text or "")
        return OMML_OPERATOR_MAP.get(operator, operator or r"\sum")

    def _omml_delimiters(self, element: ET.Element) -> tuple[str, str]:
        properties = self._omml_child(element, "dPr")
        if properties is None:
            return "(", ")"
        begin = self._omml_delimiter_value(properties, "begChr", default="(")
        end = self._omml_delimiter_value(properties, "endChr", default=")")
        return begin, end

    def _omml_delimiter_value(self, properties: ET.Element, child_name: str, *, default: str) -> str:
        child = self._omml_child(properties, child_name)
        if child is None:
            return default
        value = self._xml_attr(child, "val") or (child.text or "")
        if not value:
            return default
        return OMML_DELIMITER_MAP.get(value, value)

    def _omml_accent_command(self, element: ET.Element) -> str | None:
        properties = self._omml_child(element, "accPr")
        if properties is None:
            return None
        character = self._omml_child(properties, "chr")
        if character is None:
            return None
        value = self._xml_attr(character, "val") or (character.text or "")
        return OMML_ACCENT_MAP.get(value)

    def _latex_attach(self, *, base: str, sub: str | None = None, sup: str | None = None) -> str:
        result = f"{{{base}}}" if base else ""
        if sub:
            result += f"_{{{sub}}}"
        if sup:
            result += f"^{{{sup}}}"
        return result

    def _wrap_formula(self, formula: str, *, display: bool) -> str:
        if not formula:
            return ""
        delimiter = "$$" if display else "$"
        return f"{delimiter}{formula}{delimiter}"

    def _xml_local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _xml_namespace(self, tag: str) -> str | None:
        if tag.startswith("{") and "}" in tag:
            return tag[1:].split("}", 1)[0]
        return None

    def _xml_attr(self, element: ET.Element, attr_name: str) -> str | None:
        for key, value in element.attrib.items():
            if self._xml_local_name(key) == attr_name:
                return value
        return None
