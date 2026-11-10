"""Batch embedder with a disk cache.

- Documents use task_type RETRIEVAL_DOCUMENT, queries RETRIEVAL_QUERY (asymmetric
  retrieval, as recommended for gemini-embedding-001).
- Vectors are L2-normalised here: gemini-embedding-001 only returns unit vectors
  at its full 3072 dimensions, and we truncate to 768.
- The cache key includes model, dimension and task type, so changing any of them
  never serves a stale vector. Re-indexing unchanged docs costs nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

import numpy as np

from app.llm.client import LLM

DOC_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"


class Embedder(Protocol):
    async def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    async def embed_query(self, text: str) -> np.ndarray: ...


def l2_normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(norms == 0, 1.0, norms)


class CachedEmbedder:
    def __init__(self, llm: LLM, model: str, dim: int, cache_path: Path, batch_size: int = 50) -> None:
        self.llm = llm
        self.model = model
        self.dim = dim
        self.cache_path = cache_path
        self.batch_size = batch_size
        self.api_calls = 0
        self._cache: dict[str, list[float]] = {}
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _key(self, text: str, task: str) -> str:
        return hashlib.sha256(f"{self.model}|{self.dim}|{task}|{text}".encode()).hexdigest()

    async def _embed(self, texts: list[str], task: str) -> np.ndarray:
        keys = [self._key(t, task) for t in texts]
        todo = [(k, t) for k, t in zip(keys, texts) if k not in self._cache]
        for i in range(0, len(todo), self.batch_size):
            batch = todo[i : i + self.batch_size]
            vectors = await self.llm.embed([t for _, t in batch], task_type=task)
            self.api_calls += 1
            if len(vectors) != len(batch):
                raise RuntimeError(f"embedding API returned {len(vectors)} vectors for {len(batch)} texts")
            for (k, _), v in zip(batch, vectors):
                self._cache[k] = v
        if todo:
            self._save()
        return l2_normalize(np.array([self._cache[k] for k in keys], dtype=np.float32))

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return await self._embed(texts, DOC_TASK)

    async def embed_query(self, text: str) -> np.ndarray:
        return (await self._embed([text], QUERY_TASK))[0]
