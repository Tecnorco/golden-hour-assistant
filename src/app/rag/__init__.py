"""RAG built from scratch: loader -> chunker -> cached embedder -> numpy store -> retriever."""

from __future__ import annotations

from app.config import Settings
from app.llm.client import LLM
from app.rag.embedder import CachedEmbedder
from app.rag.retriever import Retriever
from app.rag.store import VectorStore


def make_embedder(settings: Settings, llm: LLM) -> CachedEmbedder:
    return CachedEmbedder(
        llm, settings.embedding_model, settings.embedding_dim, settings.index_dir.parent / "embed_cache.json"
    )


def load_retriever(settings: Settings, llm: LLM) -> Retriever:
    store = VectorStore.load(settings.index_dir)
    if store.meta.get("model") != settings.embedding_model or store.meta.get("dim") != settings.embedding_dim:
        raise RuntimeError("Index was built with a different embedding model/dim. Run: python -m app.cli index")
    return Retriever(store, make_embedder(settings, llm), settings.retrieval_top_k, settings.retrieval_min_score)
