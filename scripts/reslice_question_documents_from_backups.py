from __future__ import annotations

import json
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeDocument, ResourceType
from backend.services.rag_service import RagService
from scripts.backfill_question_slices import _backup_db, backfill_documents

QUESTION_RESOURCE_TYPES = {ResourceType.EXERCISE.value, ResourceType.QUESTION_SET.value}


def main() -> None:
    rag_service = RagService()
    db = SessionLocal()
    try:
        document_ids = [
            int(document.id)
            for document in db.scalars(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.resource_type.in_(tuple(QUESTION_RESOURCE_TYPES)))
                .order_by(KnowledgeDocument.id.asc())
            ).all()
        ]
    finally:
        db.close()

    backup_path = _backup_db()
    results = backfill_documents(document_ids, rag_service=rag_service)
    print(f"backup={backup_path}")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
