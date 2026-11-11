"""Vector store: a normalised numpy matrix plus chunk metadata, persisted to disk.

Files in the index directory:
  vectors.npy   float32 [n_chunks, dim], unit length
  chunks.json   list of Chunk dicts in the same row order
  meta.json     model, dim, chunking settings, build time
Cosine similarity of unit vectors is a dot product, so search is one matmul.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from app.models import Chunk, RetrievedChunk


class VectorStore:
    def __init__(self, vectors: np.ndarray, chunks: list[Chunk], meta: dict[str, Any] | None = None) -> None:
        if len(vectors) != len(chunks):
            raise ValueError("vectors and chunks must have the same length")
        self.vectors = vectors.astype(np.float32)
        self.chunks = chunks
        self.meta = meta or {}

    def search(self, query_vec: np.ndarray, k: int) -> list[RetrievedChunk]:
        if not self.chunks:
            return []
        scores = self.vectors @ query_vec.astype(np.float32)
        top = np.argsort(-scores)[:k]
        return [RetrievedChunk(**self.chunks[i].model_dump(), score=round(float(scores[i]), 4)) for i in top]

    def save(self, index_dir: Path) -> None:
        index_dir.mkdir(parents=True, exist_ok=True)
        np.save(index_dir / "vectors.npy", self.vectors)
        (index_dir / "chunks.json").write_text(
            json.dumps([c.model_dump() for c in self.chunks], indent=1), encoding="utf-8"
        )
        (index_dir / "meta.json").write_text(json.dumps(self.meta, indent=1, default=str), encoding="utf-8")

    @classmethod
    def load(cls, index_dir: Path) -> "VectorStore":
        if not (index_dir / "vectors.npy").exists():
            raise FileNotFoundError(f"No index in {index_dir}. Run: python -m app.cli index")
        vectors = np.load(index_dir / "vectors.npy")
        chunks = [Chunk(**c) for c in json.loads((index_dir / "chunks.json").read_text(encoding="utf-8"))]
        meta = json.loads((index_dir / "meta.json").read_text(encoding="utf-8"))
        return cls(vectors, chunks, meta)
