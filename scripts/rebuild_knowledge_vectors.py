"""Rebuild into an isolated collection prefix; never replace the live index.

Run in the backend container with an explicitly verified local BGE-M3 path.
Only switch CHROMADB_COLLECTION_PREFIX after the manifest passes validation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.config import Settings
from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeChunk
from backend.services.embed_service import EmbedService
from backend.services.vector_store_service import VectorStoreService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--device", default="cuda:1")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    if not Path(args.model_path).is_dir():
        parser.error("Local model directory is missing")
    live = Settings()
    if args.prefix == live.chromadb_collection_prefix:
        parser.error("Rebuild prefix must differ from the live collection prefix")
    settings = Settings(
        EMBEDDING_MODEL_NAME=args.model_path,
        EMBEDDING_DEVICE=args.device,
        EMBEDDING_BACKEND="sentence-transformers",
        EMBEDDING_DIMENSION=1024,
        EMBEDDING_BATCH_SIZE=args.batch_size,
        CHROMADB_COLLECTION_PREFIX=args.prefix,
    )
    embedder = EmbedService(settings)
    proof = embedder.embed_text("圆周运动的合力指向圆心")
    assert embedder.health_snapshot()["loaded"] and len(proof) == 1024, "Refusing to index fallback embeddings"
    vectors = VectorStoreService(settings, embedder)
    manifest = {"prefix": args.prefix, "model": args.model_path, "device": args.device, "subjects": {}}
    started = monotonic()
    with SessionLocal() as db:
        subjects = db.scalars(select(KnowledgeChunk.subject).distinct()).all()
        for subject in subjects:
            rows = db.scalars(select(KnowledgeChunk).options(selectinload(KnowledgeChunk.document)).where(KnowledgeChunk.subject == subject).order_by(KnowledgeChunk.id)).all()
            for offset in range(0, len(rows), args.batch_size):
                batch = rows[offset:offset + args.batch_size]
                vectors.upsert_chunks(subject, batch)
                if offset % (args.batch_size * 20) == 0:
                    print(json.dumps({"subject": subject, "indexed": min(offset + len(batch), len(rows)), "total": len(rows), "seconds": round(monotonic() - started, 1)}, ensure_ascii=False), flush=True)
            collection = vectors.get_collection(subject)
            indexed_ids = set(collection.get(include=[])['ids'])
            expected_ids = {str(row.id) for row in rows}
            assert indexed_ids == expected_ids, "Index contains missing or stale chunk IDs"
            matches = vectors.query(subject, rows[0].content[:500], 3) if rows else []
            assert matches or not rows, "Index query returned no matches"
            manifest["subjects"][subject] = {"database_chunks": len(rows), "vector_count": collection.count(), "sample_matches": [m.chunk_id for m in matches]}
    manifest["seconds"] = round(monotonic() - started, 2)
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
