from __future__ import annotations

import json
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeDocument


def main() -> None:
    settings = get_settings()
    upload_root = Path(settings.upload_path).resolve()
    db = SessionLocal()
    try:
        referenced = {
            Path(path).resolve()
            for path in db.scalars(select(KnowledgeDocument.file_path)).all()
            if path
        }
    finally:
        db.close()

    deleted = 0
    kept = 0
    for path in sorted(upload_root.glob("*")):
        if not path.is_file():
            continue
        if path.resolve() in referenced:
            kept += 1
            continue
        path.unlink(missing_ok=True)
        deleted += 1

    print(json.dumps({"deleted_files": deleted, "kept_files": kept, "upload_root": str(upload_root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
