from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeDocument
from backend.services.document_backup_service import DocumentBackupService


def main() -> None:
    service = DocumentBackupService()
    backup_root = service.backup_root()
    manifest_dir = backup_root / "_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = manifest_dir / f"backup-manifest-{stamp}.json"

    db = SessionLocal()
    copied = 0
    missing = 0
    manifest_docs: list[dict[str, object]] = []
    try:
        documents = db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.id.asc())).all()
        for document in documents:
            source = Path(document.file_path or "")
            destination = service.backup_path_for(document)
            destination.parent.mkdir(parents=True, exist_ok=True)

            item = {
                "document_id": document.id,
                "filename": document.filename,
                "resource_type": document.resource_type,
                "subject": document.subject,
                "mime_type": document.mime_type,
                "source_path": str(source),
                "backup_path": str(destination.relative_to(ROOT)),
                "status": "",
            }

            if not source.exists():
                item["status"] = "missing_source"
                missing += 1
                manifest_docs.append(item)
                continue

            shutil.copy2(source, destination)
            item["status"] = "copied"
            item["size_bytes"] = destination.stat().st_size
            copied += 1
            manifest_docs.append(item)

        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "backup_root": str(backup_root.relative_to(ROOT)),
            "copied_documents": copied,
            "missing_sources": missing,
            "documents": manifest_docs,
        }
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        db.close()

    print(backup_root)
    print(manifest_path)
    print(json.dumps({"copied_documents": copied, "missing_sources": missing}, ensure_ascii=False))


if __name__ == "__main__":
    main()
