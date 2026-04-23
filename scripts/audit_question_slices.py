from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeChunk, KnowledgeDocument
from backend.services.rag_service import RagService

REPORT_DIR = ROOT / ".omx" / "reports" / "question-slice-audit"
PSEUDO_FRAGMENT_PATTERN = re.compile(
    r"第\d+题\s+题目：\s*(?:\d+\s*[A-DＡ-Ｄ]|[A-DＡ-Ｄ]\s*[.．、]|(?:\d+\s*)?[：:]\s*\d+|\d+\s*[）)])"
)
FILL_IN_FRAGMENT_PATTERN = re.compile(r"(?:_{3,}|略。|填空|运动与静止|垃圾分类|坐标系|位移和路程)")
ANSWER_MARKER_PATTERN = re.compile(r"(?:【)?(?:参考)?答案(?:】)?\s*(?:[:：])?")
EXPLANATION_MARKER_PATTERN = re.compile(r"(?:【)?(?:解析|详解|解答|思路(?:点拨)?|点拨|说明|分析|点睛)(?:】)?\s*(?:[:：])?")


@dataclass(slots=True)
class ChunkCounts:
    questions: int
    answers: int
    explanations: int

    def as_dict(self) -> dict[str, int]:
        return {
            "questions": self.questions,
            "answers": self.answers,
            "explanations": self.explanations,
        }


def _chunk_counts_from_rows(rows: list[KnowledgeChunk]) -> ChunkCounts:
    return ChunkCounts(
        questions=len(rows),
        answers=sum(1 for row in rows if str((row.metadata_json or {}).get("answer_text") or "").strip()),
        explanations=sum(1 for row in rows if str((row.metadata_json or {}).get("explanation_text") or "").strip()),
    )


def _chunk_counts_from_prepared(chunks: list[Any]) -> ChunkCounts:
    return ChunkCounts(
        questions=len(chunks),
        answers=sum(1 for chunk in chunks if str((chunk.metadata or {}).get("answer_text") or "").strip()),
        explanations=sum(1 for chunk in chunks if str((chunk.metadata or {}).get("explanation_text") or "").strip()),
    )


def _suspect_chunks(rows: list[KnowledgeChunk]) -> list[dict[str, Any]]:
    numbers = Counter(str((row.metadata_json or {}).get("question_number") or "").strip() for row in rows)
    rows_by_number: dict[str, list[KnowledgeChunk]] = {}
    for row in rows:
        number = str((row.metadata_json or {}).get("question_number") or "").strip()
        if number:
            rows_by_number.setdefault(number, []).append(row)
    suspects: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.metadata_json or {}
        number = str(metadata.get("question_number") or "").strip()
        content = str(row.content or "")
        summary = " ".join(content.splitlines()[:5]).strip()[:140]
        has_answer = bool(str(metadata.get("answer_text") or "").strip())
        has_explanation = bool(str(metadata.get("explanation_text") or "").strip())
        flags: list[str] = []
        if number and numbers[number] > 1:
            flags.append("raw_duplicate_question_number")
            source_locators = {
                str((candidate.metadata_json or {}).get("source_locator") or "").strip()
                for candidate in rows_by_number.get(number, [])
            }
            question_uids = {
                str((candidate.metadata_json or {}).get("question_uid") or "").strip()
                for candidate in rows_by_number.get(number, [])
            }
            if "" in source_locators or len(source_locators) < numbers[number]:
                flags.append("non_expected_duplicate_logical_item")
            if "" in question_uids or len(question_uids) < numbers[number]:
                flags.append("same_document_question_uid_collision")
        if not has_answer:
            flags.append("missing_answer")
        if not has_explanation:
            flags.append("missing_explanation")
        if PSEUDO_FRAGMENT_PATTERN.search(summary):
            flags.append("pseudo_tail_fragment")
        if FILL_IN_FRAGMENT_PATTERN.search(summary):
            flags.append("review_or_fillin_fragment")
        if flags:
            suspects.append(
                {
                    "chunk_index": row.chunk_index,
                    "question_number": number or None,
                    "flags": flags,
                    "summary": summary,
                }
            )
    return suspects


def _suspect_prepared_chunks(chunks: list[Any]) -> list[dict[str, Any]]:
    numbers = Counter(str((chunk.metadata or {}).get("question_number") or "").strip() for chunk in chunks)
    chunks_by_number: dict[str, list[Any]] = {}
    for chunk in chunks:
        number = str((chunk.metadata or {}).get("question_number") or "").strip()
        if number:
            chunks_by_number.setdefault(number, []).append(chunk)
    suspects: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        metadata = chunk.metadata or {}
        number = str(metadata.get("question_number") or "").strip()
        content = str(chunk.content or "")
        summary = " ".join(content.splitlines()[:5]).strip()[:140]
        has_answer = bool(str(metadata.get("answer_text") or "").strip())
        has_explanation = bool(str(metadata.get("explanation_text") or "").strip())
        flags: list[str] = []
        if number and numbers[number] > 1:
            flags.append("raw_duplicate_question_number")
            source_locators = {
                str((candidate.metadata or {}).get("source_locator") or "").strip()
                for candidate in chunks_by_number.get(number, [])
            }
            question_uids = {
                str((candidate.metadata or {}).get("question_uid") or "").strip()
                for candidate in chunks_by_number.get(number, [])
            }
            if "" in source_locators or len(source_locators) < numbers[number]:
                flags.append("non_expected_duplicate_logical_item")
            if "" in question_uids or len(question_uids) < numbers[number]:
                flags.append("same_document_question_uid_collision")
        if not has_answer:
            flags.append("missing_answer")
        if not has_explanation:
            flags.append("missing_explanation")
        if PSEUDO_FRAGMENT_PATTERN.search(summary):
            flags.append("pseudo_tail_fragment")
        if FILL_IN_FRAGMENT_PATTERN.search(summary):
            flags.append("review_or_fillin_fragment")
        if flags:
            suspects.append(
                {
                    "chunk_index": index,
                    "question_number": number or None,
                    "flags": flags,
                    "summary": summary,
                }
            )
    return suspects


def _classify_document(
    *,
    db_counts: ChunkCounts,
    reparse_counts: ChunkCounts | None,
    extract_text: str,
    suspects: list[dict[str, Any]],
    reparse_error: str | None,
) -> tuple[str, list[str], list[str]]:
    if reparse_error:
        return "reparse_error", ["best_effort_reparse_failed"], ["repair audit runtime or permissions before trusting source classification"]

    assert reparse_counts is not None
    suspect_flags = Counter(flag for suspect in suspects for flag in suspect["flags"])
    missing_answer_only = sum(
        1
        for suspect in suspects
        if "missing_answer" in suspect["flags"] and "missing_explanation" not in suspect["flags"]
    )
    answer_markers = len(ANSWER_MARKER_PATTERN.findall(extract_text))
    explanation_markers = len(EXPLANATION_MARKER_PATTERN.findall(extract_text))

    reasons: list[str] = []
    fixes: list[str] = []

    if reparse_counts.questions == reparse_counts.answers == reparse_counts.explanations:
        reasons.append("best_effort_reparse_is_aligned")
        if suspect_flags["non_expected_duplicate_logical_item"] or suspect_flags["pseudo_tail_fragment"]:
            reasons.append("stored_chunks_show_tail_fragment_false_split")
            fixes.append("strengthen _parse_numbered_blocks() and repeated-answer-bank merge guards")
        if suspect_flags["same_document_question_uid_collision"]:
            reasons.append("same_document_question_uid_collision")
            fixes.append("enforce deterministic source_locator/question_uid collision suffixing")
        return "historical_parser_bug_or_stale_db", reasons, fixes or ["reingest affected docs after parser fix lands"]

    if suspect_flags["non_expected_duplicate_logical_item"] or suspect_flags["pseudo_tail_fragment"]:
        reasons.append("tail_fragment_or_repeated_number_false_split")
        fixes.append("tighten numbered-block splitting and repeated-answer-bank absorption")

    if suspect_flags["same_document_question_uid_collision"]:
        reasons.append("same_document_question_uid_collision")
        fixes.append("enforce deterministic source_locator/question_uid collision suffixing")

    if suspect_flags["review_or_fillin_fragment"]:
        reasons.append("review_or_fillin_segments_miscollected_as_question_items")
        fixes.append("add diagnostics/filters for non-question review or fill-in fragments")

    if (
        reparse_counts.explanations == reparse_counts.questions
        and reparse_counts.answers < reparse_counts.questions
        and missing_answer_only == (reparse_counts.questions - reparse_counts.answers)
    ):
        reasons.append("source_material_uses_blank_answer_with_explanation_only")
        fixes.append("allowed_by_product_rule_missing_answer_with_explanation")
        return "allowed_missing_answer_with_explanation", reasons, fixes

    answer_only_gap = (
        reparse_counts.explanations == reparse_counts.questions
        and reparse_counts.answers < reparse_counts.questions
        and missing_answer_only == (reparse_counts.questions - reparse_counts.answers)
    )

    if reparse_counts.answers < reparse_counts.questions or reparse_counts.explanations < reparse_counts.questions:
        if answer_markers < reparse_counts.questions or explanation_markers < reparse_counts.questions:
            reasons.append("source_material_lacks_complete_answer_or_explanation_markers")
            fixes.append("exclude as source-missing unless manual review proves parser loss")
        elif not answer_only_gap:
            reasons.append("answer_explanation_pairing_still_incomplete")
            fixes.append("refine grouped-answer parsing and section attribution helpers")

    if not reasons:
        reasons.append("unclassified_mismatch")
        fixes.append("manual review needed")

    if "source_material_lacks_complete_answer_or_explanation_markers" in reasons and "tail_fragment_or_repeated_number_false_split" not in reasons:
        return "source_missing_excluded", reasons, fixes
    return "parser_bug_remaining", reasons, fixes


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = REPORT_DIR / f"question-slice-audit-{report_tag}.json"
    md_path = REPORT_DIR / f"question-slice-audit-{report_tag}.md"

    rag_service = RagService()
    db = SessionLocal()

    try:
        documents = db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.id.asc())).all()
        results: list[dict[str, Any]] = []
        summary = Counter()

        for document in documents:
            rows = db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id)
                .order_by(KnowledgeChunk.chunk_index.asc())
            ).all()
            question_rows = [row for row in rows if str((row.metadata_json or {}).get("chunk_kind") or "").strip() == "question_item"]
            if not question_rows:
                continue

            db_counts = _chunk_counts_from_rows(question_rows)
            if db_counts.questions == db_counts.answers == db_counts.explanations:
                continue

            suspects = _suspect_chunks(question_rows)
            extract_text = ""
            reparse_counts: ChunkCounts | None = None
            reparse_error: str | None = None
            reparsed_suspects: list[dict[str, Any]] | None = None
            try:
                temp_document = KnowledgeDocument(
                    id=900000 + int(document.id or 0),
                    subject=document.subject,
                    filename=document.filename,
                    file_path=document.file_path,
                    mime_type=document.mime_type,
                    size_bytes=document.size_bytes,
                    resource_type=document.resource_type,
                    chapter=document.chapter,
                    section=document.section,
                    difficulty=document.difficulty,
                    tags_json=document.tags_json,
                )
                extracted = rag_service.extract_content(
                    document.file_path,
                    mime_type=document.mime_type,
                    document_id=temp_document.id,
                    task_id=temp_document.id,
                    resource_type=temp_document.resource_type,
                )
                extract_text = extracted.text
                reparsed = rag_service.prepare_document_chunks(
                    temp_document,
                    extracted.text,
                    assets=extracted.assets,
                    parsed_pdf=extracted.parsed_pdf,
                    parser_backend=extracted.parser_backend,
                    parser_provenance=extracted.parser_provenance,
                    source_format=extracted.source_format,
                )
                reparsed_question_chunks = [
                    chunk
                    for chunk in reparsed
                    if str((chunk.metadata or {}).get("chunk_kind") or "").strip() == "question_item"
                ]
                reparse_counts = _chunk_counts_from_prepared(reparsed_question_chunks)
                reparsed_suspects = _suspect_prepared_chunks(reparsed_question_chunks)
            except Exception as exc:  # pragma: no cover - best effort runtime path
                try:
                    extracted = rag_service.extract_content(document.file_path, mime_type=document.mime_type)
                    extract_text = extracted.text
                    reparsed = rag_service.prepare_document_chunks(
                        document,
                        extracted.text,
                        assets=extracted.assets,
                        parsed_pdf=extracted.parsed_pdf,
                        parser_backend=extracted.parser_backend,
                        parser_provenance=extracted.parser_provenance,
                        source_format=extracted.source_format,
                    )
                    reparsed_question_chunks = [
                        chunk
                        for chunk in reparsed
                        if str((chunk.metadata or {}).get("chunk_kind") or "").strip() == "question_item"
                    ]
                    reparse_counts = _chunk_counts_from_prepared(reparsed_question_chunks)
                    reparsed_suspects = _suspect_prepared_chunks(reparsed_question_chunks)
                    reparse_error = f"question_resource_reparse_failed:{type(exc).__name__}: {exc}; fallback=legacy_extract"
                except Exception as fallback_exc:
                    reparse_error = f"{type(exc).__name__}: {exc}; fallback_failed={type(fallback_exc).__name__}: {fallback_exc}"

            classification_suspects = reparsed_suspects if reparsed_suspects is not None else suspects

            classification, reasons, fixes = _classify_document(
                db_counts=db_counts,
                reparse_counts=reparse_counts,
                extract_text=extract_text,
                suspects=classification_suspects,
                reparse_error=reparse_error,
            )
            summary[classification] += 1
            results.append(
                {
                    "document_id": document.id,
                    "filename": document.filename,
                    "resource_type": document.resource_type,
                    "db_counts": db_counts.as_dict(),
                    "reparse_counts": reparse_counts.as_dict() if reparse_counts else None,
                    "classification": classification,
                    "reasons": reasons,
                    "suggested_fix_tracks": fixes,
                    "db_suspect_chunks": suspects[:12],
                    "reparse_suspect_chunks": (reparsed_suspects or [])[:12],
                    "reparse_error": reparse_error,
                }
            )

        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": dict(summary),
            "documents": results,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# Question Slice Audit",
            "",
            f"- Generated at: {payload['generated_at']}",
            f"- Mismatch documents: {len(results)}",
        ]
        for label, count in sorted(summary.items()):
            lines.append(f"- {label}: {count}")
        lines.append("")
        for item in results:
            lines.extend(
                [
                    f"## {item['document_id']} — {item['filename']}",
                    f"- resource_type: {item['resource_type']}",
                    f"- db_counts: {item['db_counts']}",
                    f"- reparse_counts: {item['reparse_counts']}",
                    f"- classification: {item['classification']}",
                    f"- reasons: {', '.join(item['reasons'])}",
                    f"- suggested_fix_tracks: {', '.join(item['suggested_fix_tracks'])}",
                ]
            )
            if item["reparse_error"]:
                lines.append(f"- reparse_error: {item['reparse_error']}")
            if item["db_suspect_chunks"]:
                lines.append("- db_suspect_chunks:")
                for suspect in item["db_suspect_chunks"]:
                    lines.append(
                        "  - "
                        f"chunk={suspect['chunk_index']} "
                        f"q={suspect['question_number']} "
                        f"flags={','.join(suspect['flags'])} "
                        f"summary={suspect['summary']}"
                    )
            if item["reparse_suspect_chunks"]:
                lines.append("- reparse_suspect_chunks:")
                for suspect in item["reparse_suspect_chunks"]:
                    lines.append(
                        "  - "
                        f"chunk={suspect['chunk_index']} "
                        f"q={suspect['question_number']} "
                        f"flags={','.join(suspect['flags'])} "
                        f"summary={suspect['summary']}"
                    )
            lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")

        print(json_path)
        print(md_path)
        print(json.dumps(dict(summary), ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
