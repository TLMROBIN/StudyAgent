from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from backend.config import Settings, get_settings
from backend.services.embed_service import EmbedService, embed_service

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.models.knowledge import KnowledgeChunk


SUBJECT_COLLECTION_NAMES = {
    "语文": "chinese",
    "数学": "math",
    "英语": "english",
    "物理": "physics",
    "化学": "chemistry",
    "生物": "biology",
    "政治": "politics",
    "历史": "history",
    "地理": "geography",
}


@dataclass
class VectorMatch:
    chunk_id: int
    distance: float | None = None
    metadata: dict[str, Any] | None = None


class VectorStoreService:
    def __init__(self, settings: Settings | None = None, embedder: EmbedService | None = None) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or embed_service
        self._client = None
        self._backend_name = "chromadb-http" if self.settings.chromadb_mode == "http" else "chromadb-persistent"

    def get_collection(self, subject: str):
        client = self._ensure_client()
        collection_name = self._collection_name(subject)
        return client.get_or_create_collection(
            name=collection_name,
            metadata={"subject": subject, "distance": "cosine"},
        )

    def upsert_chunks(self, subject: str, chunks: list["KnowledgeChunk"]) -> None:
        if not chunks:
            return
        collection = self.get_collection(subject)
        documents = [chunk.content for chunk in chunks]
        embeddings = self.embedder.embed_texts(documents)
        collection.upsert(
            ids=[str(chunk.id) for chunk in chunks],
            documents=documents,
            embeddings=embeddings,
            metadatas=[self._build_metadata(chunk) for chunk in chunks],
        )

    def delete_document(self, subject: str, document_id: int) -> None:
        try:
            collection = self.get_collection(subject)
            collection.delete(where={"document_id": str(document_id)})
        except Exception as exc:
            logger.warning("Failed to delete vectors for document %s: %s", document_id, exc)

    def query(self, subject: str, question: str, top_k: int) -> list[VectorMatch]:
        collection = self.get_collection(subject)
        query_embedding = self.embedder.embed_text(question)
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["metadatas", "distances"],
        )
        ids = result.get("ids", [[]])[0]
        distances = result.get("distances", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        matches: list[VectorMatch] = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = distances[index] if index < len(distances) else None
            matches.append(VectorMatch(chunk_id=int(chunk_id), distance=distance, metadata=metadata or {}))
        return matches

    def health_snapshot(self) -> dict[str, str | int | bool]:
        return {
            "backend": self._backend_name,
            "mode": self.settings.chromadb_mode,
            "loaded": self._client is not None,
            "path": self.settings.chromadb_path if self.settings.chromadb_mode == "persistent" else "",
            "host": self.settings.chromadb_host if self.settings.chromadb_mode == "http" else "",
            "port": self.settings.chromadb_port if self.settings.chromadb_mode == "http" else 0,
        }

    def _ensure_client(self):
        if self._client is not None:
            return self._client

        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError("chromadb 未安装，无法初始化向量库") from exc

        if self.settings.chromadb_mode == "http":
            self._client = chromadb.HttpClient(
                host=self.settings.chromadb_host,
                port=self.settings.chromadb_port,
                ssl=self.settings.chromadb_ssl,
            )
            self._backend_name = "chromadb-http"
        else:
            self._client = chromadb.PersistentClient(path=self.settings.chromadb_path)
            self._backend_name = "chromadb-persistent"
        return self._client

    def _collection_name(self, subject: str) -> str:
        safe_subject = SUBJECT_COLLECTION_NAMES.get(subject, subject.lower())
        return f"{self.settings.chromadb_collection_prefix}-{safe_subject}"

    def _build_metadata(self, chunk: "KnowledgeChunk") -> dict[str, Any]:
        metadata = {
            "chunk_id": chunk.id,
            "document_id": str(chunk.document_id),
            "subject": chunk.subject,
            "chunk_index": chunk.chunk_index,
        }
        source = chunk.metadata_json or {}
        scalar_keys = ("resource_type", "grade", "chapter", "section", "difficulty", "chunk_kind", "question_number")
        for key in scalar_keys:
            value = source.get(key)
            if value is None or value == "":
                continue
            metadata[key] = value
        tags = source.get("tags") or []
        if isinstance(tags, list) and tags:
            metadata["tags_text"] = ",".join(str(item) for item in tags[:8])
        return metadata


vector_store_service = VectorStoreService()
