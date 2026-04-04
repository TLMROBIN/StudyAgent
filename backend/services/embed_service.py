from __future__ import annotations

import hashlib
import logging
import math
import re
from threading import Lock
from typing import Any

from backend.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmbedService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model: Any | None = None
        self._backend_name = self.settings.embedding_backend
        self._device = self.settings.embedding_device
        self._load_attempted = False
        self._lock = Lock()

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        normalized_texts = [self._normalize_text_for_embedding(text) for text in texts]
        model = self._ensure_model()
        if model is None:
            return [self._hash_embed_text(text) for text in normalized_texts]

        vectors = model.encode(
            normalized_texts,
            batch_size=self.settings.embedding_batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return [vector.tolist() for vector in vectors]

    def health_snapshot(self) -> dict[str, str | int | bool]:
        return {
            "backend": self._backend_name,
            "device": self._device,
            "model": self.settings.embedding_model_name if self._backend_name != "hash-fallback" else "hash-fallback",
            "loaded": self._model is not None,
            "load_attempted": self._load_attempted,
            "fallback_enabled": self.settings.embedding_fallback_to_hash,
        }

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model
            if self.settings.embedding_backend != "sentence-transformers":
                self._load_attempted = True
                self._backend_name = "hash-fallback"
                self._device = "cpu"
                return None
            self._load_attempted = True
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                logger.warning("sentence-transformers not installed, using hash fallback embedding")
                self._backend_name = "hash-fallback"
                self._device = "cpu"
                return None

            try:
                device = self._resolve_device()
                self._model = SentenceTransformer(self.settings.embedding_model_name, device=device)
                self._backend_name = "sentence-transformers"
                self._device = device
                logger.info("Loaded embedding model %s on %s", self.settings.embedding_model_name, device)
                return self._model
            except Exception as exc:
                logger.warning("Embedding model load failed, using hash fallback: %s", exc)
                self._backend_name = "hash-fallback"
                self._device = "cpu"
                self._model = None
                return None

    def _resolve_device(self) -> str:
        if self.settings.embedding_device != "auto":
            return self.settings.embedding_device

        try:
            import torch
        except ImportError:
            return "cpu"

        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def _hash_embed_text(self, text: str) -> list[float]:
        dims = self.settings.embedding_dimension
        vector = [0.0] * dims
        for token in self._tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % dims
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    @staticmethod
    def cosine_similarity(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=False))

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        clean = text.replace("\n", " ").strip()
        if not clean:
            return []
        return [clean[i : i + 2] for i in range(max(len(clean) - 1, 1))]

    @staticmethod
    def _normalize_text_for_embedding(text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        latex_replacements = {
            "$$": " ",
            "$": " ",
            r"\[": " ",
            r"\]": " ",
            r"\(": " ",
            r"\)": " ",
            r"\cdot": " 点乘 ",
            r"\times": " 乘 ",
            r"\div": " 除 ",
            r"\pm": " 正负 ",
            r"\mp": " 负正 ",
            r"\leq": " 小于等于 ",
            r"\geq": " 大于等于 ",
            r"\neq": " 不等于 ",
            r"\approx": " 约等于 ",
            r"\sim": " 相似 ",
            r"\triangle": " 三角形 ",
            r"\angle": " 角 ",
            r"\sin": " 正弦 ",
            r"\cos": " 余弦 ",
            r"\tan": " 正切 ",
            r"\log": " 对数 ",
            r"\ln": " 自然对数 ",
            r"\sum": " 求和 ",
            r"\int": " 积分 ",
            r"\lim": " 极限 ",
            r"\frac": " 分式 ",
            r"\sqrt": " 根号 ",
            r"\alpha": " alpha ",
            r"\beta": " beta ",
            r"\gamma": " gamma ",
            r"\delta": " delta ",
            r"\theta": " theta ",
            r"\lambda": " lambda ",
            r"\mu": " mu ",
            r"\omega": " omega ",
            r"\pi": " pi ",
            r"\Delta": " delta ",
        }
        for source, target in latex_replacements.items():
            normalized = normalized.replace(source, target)

        normalized = re.sub(r"([A-Za-z0-9)\]])\s*\^\s*\{?2\}?", r"\1 平方 ", normalized)
        normalized = re.sub(r"([A-Za-z0-9)\]])\s*\^\s*\{?3\}?", r"\1 立方 ", normalized)
        normalized = re.sub(r"([A-Za-z0-9)\]])\s*_\s*\{?([^{}\s]{1,8})\}?", r"\1 下标 \2 ", normalized)
        normalized = re.sub(r"\\[A-Za-z]+", " ", normalized)
        normalized = normalized.replace("{", " ").replace("}", " ").replace("^", " ").replace("_", " ")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized or text.strip()


embed_service = EmbedService()
