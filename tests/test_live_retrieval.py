"""Live: real embeddings, real index (built in a temp dir). Run with: pytest -m live"""

import asyncio

import pytest

from app.config import get_settings
from app.rag import load_retriever, make_embedder
from app.rag.retriever import build_index

pytestmark = pytest.mark.live


def test_live_index_and_retrieve():
    s = get_settings()
    if s.api_key is None:
        pytest.skip("GEMINI_API_KEY not set")
    from app.llm.client import GeminiLLM

    llm = GeminiLLM(s)
    asyncio.run(build_index(s, make_embedder(s, llm))).save(s.index_dir)
    r = load_retriever(s, llm)
    hits = asyncio.run(r.retrieve("If I cancel, do I get my deposit back?"))
    assert hits and hits[0].source == "cancellation-and-rescheduling"
    assert asyncio.run(r.retrieve("What is the capital of Australia?")) == []
