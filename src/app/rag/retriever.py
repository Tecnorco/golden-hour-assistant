"""Build the index, retrieve with a score threshold, format chunks for citation."""

from __future__ import annotations

from app.config import Settings
from app.models import Chunk, RetrievedChunk, utcnow
from app.observability.tracing import span
from app.rag.chunker import chunk_documents
from app.rag.embedder import Embedder
from app.rag.loader import load_documents
from app.rag.store import VectorStore


def embedding_text(chunk: Chunk, doc_title: str) -> str:
    # Title and heading give a short section the context it lacks on its own.
    return f"{doc_title} - {chunk.heading}\n\n{chunk.text}"


async def build_index(settings: Settings, embedder: Embedder) -> VectorStore:
    with span("rag.build_index") as s:
        docs = load_documents(settings.data_dir / "docs")
        titles = {d.source: d.title for d in docs}
        chunks = chunk_documents(
            docs,
            target_tokens=settings.chunk_target_tokens,
            min_tokens=settings.chunk_min_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
        vectors = await embedder.embed_documents([embedding_text(c, titles[c.source]) for c in chunks])
        store = VectorStore(
            vectors,
            chunks,
            meta={
                "model": settings.embedding_model,
                "dim": settings.embedding_dim,
                "chunk_target_tokens": settings.chunk_target_tokens,
                "chunk_min_tokens": settings.chunk_min_tokens,
                "chunk_overlap_tokens": settings.chunk_overlap_tokens,
                "documents": len(docs),
                "chunks": len(chunks),
                "built_at": utcnow().isoformat(),
            },
        )
        s.output = {"documents": len(docs), "chunks": len(chunks)}
        return store


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder, top_k: int = 4, min_score: float = 0.62) -> None:
        self.store = store
        self.embedder = embedder
        self.top_k = top_k
        self.min_score = min_score

    async def retrieve(self, query: str, k: int | None = None, min_score: float | None = None) -> list[RetrievedChunk]:
        """Top-k chunks scoring at least `min_score`. An empty list means
        "we do not know"; callers must then escalate, never improvise."""
        k = k or self.top_k
        threshold = self.min_score if min_score is None else min_score
        with span("rag.retrieve", input=query) as s:
            qvec = await self.embedder.embed_query(query)
            candidates = self.store.search(qvec, k)
            hits = [c for c in candidates if c.score >= threshold]
            s.set(
                threshold=threshold,
                candidates=[{"chunk_id": c.chunk_id, "score": c.score} for c in candidates],
            )
            s.output = [f"{c.chunk_id} ({c.score})" for c in hits] or "NO_HITS"
            return hits


def format_context(chunks: list[RetrievedChunk]) -> str:
    """Render chunks for a prompt. The agent must cite the ids in square brackets."""
    if not chunks:
        return "(no relevant policy found)"
    return "\n\n".join(f"[{c.chunk_id}] ({c.source} > {c.heading})\n{c.text}" for c in chunks)
