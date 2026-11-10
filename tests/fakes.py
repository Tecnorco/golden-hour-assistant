"""Deterministic test doubles for the RAG layer."""

from __future__ import annotations

import hashlib
import re

import numpy as np

from app.rag.embedder import l2_normalize

DIM = 256
_STOP = {"the", "a", "an", "of", "to", "and", "is", "i", "my", "do", "you", "for", "in", "on", "can", "we", "our", "what", "how"}


def bow_vector(text: str) -> np.ndarray:
    """Hashed bag-of-words: texts sharing words get similar vectors."""
    v = np.zeros(DIM, dtype=np.float32)
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w not in _STOP:
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    return v


class FakeEmbedLLM:
    """Implements LLM.embed only; counts calls so cache behaviour can be tested."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        self.calls.append((len(texts), task_type))
        return [(bow_vector(t) * 3.0).tolist() for t in texts]  # deliberately not unit length


class FakeEmbedder:
    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return l2_normalize(np.array([bow_vector(t) for t in texts]))

    async def embed_query(self, text: str) -> np.ndarray:
        return l2_normalize(bow_vector(text)[None, :])[0]
