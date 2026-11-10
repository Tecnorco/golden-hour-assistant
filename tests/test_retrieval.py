import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.config import get_settings
from app.evaluation.retrieval import run_retrieval_eval, to_markdown
from app.models import Chunk
from app.rag import load_retriever
from app.rag.embedder import DOC_TASK, QUERY_TASK, CachedEmbedder
from app.rag.retriever import Retriever, build_index, format_context
from app.rag.store import VectorStore
from fakes import FakeEmbedder, FakeEmbedLLM

CHUNKS = [
    Chunk(chunk_id="pay#deposit#0", source="pay", heading="Deposit", text="A 30% deposit is required to book. The deposit is non-refundable."),
    Chunk(chunk_id="wear#clothes#0", source="wear", heading="Clothes", text="Wear solid colours and a matching colour palette for family photos."),
    Chunk(chunk_id="dl#wedding#0", source="dl", heading="Weddings", text="Wedding galleries are delivered within 6 weeks."),
]


def _retriever(min_score: float = 0.3) -> Retriever:
    emb = FakeEmbedder()
    vecs = asyncio.run(emb.embed_documents([c.text for c in CHUNKS]))
    return Retriever(VectorStore(vecs, CHUNKS, {"model": "fake", "dim": 256}), emb, top_k=2, min_score=min_score)


def test_retrieves_relevant_chunk_first_with_citation_fields():
    hits = asyncio.run(_retriever().retrieve("is the deposit refundable"))
    assert hits[0].chunk_id == "pay#deposit#0"
    assert hits[0].source == "pay" and hits[0].heading == "Deposit" and 0 < hits[0].score <= 1


def test_threshold_returns_nothing_for_off_topic_query(sink):
    hits = asyncio.run(_retriever().retrieve("capital of australia"))
    assert hits == []
    span = next(s for s in sink.spans if s.name == "rag.retrieve")
    assert span.output_summary == "NO_HITS" and "candidates" in span.attributes


def test_top_k_is_respected():
    hits = asyncio.run(_retriever(min_score=-1).retrieve("deposit wedding colour", k=2))
    assert len(hits) == 2


def test_format_context_lists_ids_for_citation():
    hits = asyncio.run(_retriever().retrieve("wedding galleries delivered"))
    ctx = format_context(hits)
    assert ctx.startswith("[dl#wedding#0] (dl > Weddings)")
    assert format_context([]) == "(no relevant policy found)"


def test_store_roundtrip(tmp_path):
    r = _retriever()
    r.store.save(tmp_path)
    loaded = VectorStore.load(tmp_path)
    assert loaded.chunks == CHUNKS and np.allclose(loaded.vectors, r.store.vectors)


def test_load_missing_index_has_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="app.cli index"):
        VectorStore.load(tmp_path / "nope")


def test_embedder_caches_normalises_and_separates_task_types(tmp_path):
    llm = FakeEmbedLLM()
    emb = CachedEmbedder(llm, "m", 256, tmp_path / "cache.json", batch_size=2)
    v = asyncio.run(emb.embed_documents(["a b", "c d", "e f"]))
    assert llm.calls == [(2, DOC_TASK), (1, DOC_TASK)]  # batched
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0)  # normalised
    asyncio.run(emb.embed_documents(["a b", "c d", "e f"]))
    assert len(llm.calls) == 2  # all from cache
    asyncio.run(emb.embed_query("a b"))
    assert llm.calls[-1] == (1, QUERY_TASK)  # same text, other task type -> new key
    # cache persists across instances
    emb2 = CachedEmbedder(llm, "m", 256, tmp_path / "cache.json")
    asyncio.run(emb2.embed_documents(["a b"]))
    assert len(llm.calls) == 3
    # a different model never reuses vectors
    asyncio.run(CachedEmbedder(llm, "other", 256, tmp_path / "cache.json").embed_documents(["a b"]))
    assert len(llm.calls) == 4


def test_build_index_over_real_docs_and_reload_guard(tmp_path, monkeypatch):
    s = get_settings()
    store = asyncio.run(build_index(s, FakeEmbedder()))
    assert store.meta["chunks"] == len(store.chunks) > 20
    store.meta["model"] = "some-other-model"
    store.save(s.index_dir)
    with pytest.raises(RuntimeError, match="different embedding model"):
        load_retriever(s, llm=None)  # type: ignore[arg-type]


def test_retrieval_eval_metrics():
    data = {
        "pairs": [
            {"q": "deposit refundable", "source": "pay", "phrase": "non-refundable"},
            {"q": "what colours to wear", "source": "wear", "phrase": "colour palette"},
            {"q": "wedding delivery", "source": "pay", "phrase": "not there"},
        ],
        "negatives": ["capital of australia"],
    }
    r = asyncio.run(run_retrieval_eval(_retriever(), data, k=2))
    assert r.hit_rate == pytest.approx(2 / 3, abs=1e-3)
    assert r.pairs[0].rank == 1 and r.pairs[2].rank is None
    assert r.negatives_false_hit_rate == 0.0
    assert "Hit rate @2" in to_markdown(r)
