from __future__ import annotations

import json
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeDocument
from backend.services.document_backup_service import DocumentBackupService


def _latest_manifest() -> Path:
    manifest_dir = ROOT / "data" / "document_backups" / "_manifests"
    manifests = sorted(manifest_dir.glob("backup-manifest-*.json"))
    if not manifests:
        raise FileNotFoundError("no backup manifest found")
    return manifests[-1]


def main() -> None:
    manifest_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _latest_manifest()
    service = DocumentBackupService()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapping = {
        int(item["document_id"]): ROOT / str(item["backup_path"])
        for item in payload.get("documents", [])
        if item.get("status") == "copied"
    }

    updated = 0
    skipped = 0
    db = SessionLocal()
    try:
        documents = db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.id.asc())).all()
        for document in documents:
            backup_path = mapping.get(int(document.id))
            if backup_path is None or not backup_path.exists():
                skipped += 1
                continue
            new_path = service.to_storage_path(backup_path)
            current_resolved = service.resolve_path(document.file_path)
            if current_resolved.resolve() == backup_path.resolve() and document.file_path == new_path:
                skipped += 1
                continue
            document.file_path = new_path
            db.add(document)
            updated += 1
        db.commit()
    finally:
        db.close()

    print(manifest_path)
    print(json.dumps({"updated_documents": updated, "skipped_documents": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
