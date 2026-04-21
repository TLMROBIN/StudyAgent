from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeDocument
from backend.services.rag_service import RagService

DEFAULT_CLASSIFICATIONS = {"historical_parser_bug_or_stale_db"}


def _latest_audit_report() -> Path:
    report_dir = ROOT / ".omx" / "reports" / "question-slice-audit"
    matches = sorted(report_dir.glob("question-slice-audit-*.json"))
    if not matches:
        raise FileNotFoundError("no question-slice audit report found")
    return matches[-1]


def _backup_db() -> Path:
    source = ROOT / "data" / "studyagent.db"
    if not source.exists():
        raise FileNotFoundError(source)
    backup_dir = ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"studyagent-before-question-slice-backfill-{stamp}.db"
    shutil.copy2(source, target)
    return target


def _selected_document_ids(report_path: Path, classifications: set[str]) -> list[int]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    return [
        int(item["document_id"])
        for item in payload.get("documents", [])
        if str(item.get("classification") or "") in classifications
    ]


def _synthetic_document(document: KnowledgeDocument) -> KnowledgeDocument:
    return KnowledgeDocument(
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


def backfill_documents(document_ids: Iterable[int], *, rag_service: RagService) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    db = SessionLocal()
    try:
        for document_id in document_ids:
            document = db.get(KnowledgeDocument, document_id)
            if document is None:
                results.append({"document_id": document_id, "status": "missing_document"})
                continue

            synthetic = _synthetic_document(document)
            extracted = rag_service.extract_content(
                document.file_path,
                mime_type=document.mime_type,
                document_id=synthetic.id,
                task_id=synthetic.id,
                resource_type=synthetic.resource_type,
            )
            prepared_chunks = rag_service.prepare_document_chunks(
                synthetic,
                extracted.text,
                assets=extracted.assets,
                parsed_pdf=extracted.parsed_pdf,
                parser_backend=extracted.parser_backend,
                parser_provenance=extracted.parser_provenance,
                source_format=extracted.source_format,
            )
            count = rag_service.ingest_document_chunks(db, document, prepared_chunks)
            results.append(
                {
                    "document_id": document.id,
                    "filename": document.filename,
                    "status": "reingested",
                    "chunk_count": count,
                }
            )
    finally:
        db.close()
    return results


def main() -> None:
    report_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _latest_audit_report()
    selected_ids = _selected_document_ids(report_path, DEFAULT_CLASSIFICATIONS)
    if not selected_ids:
        print("no documents selected")
        return

    backup_path = _backup_db()
    rag_service = RagService()
    results = backfill_documents(selected_ids, rag_service=rag_service)
    print(f"backup={backup_path}")
    print(f"report={report_path}")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
