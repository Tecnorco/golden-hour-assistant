from pathlib import Path

from app.rag.chunker import chunk_document, chunk_documents, estimate_tokens, split_sections
from app.rag.loader import Document, load_documents

DOCS = Path(__file__).resolve().parents[1] / "data" / "docs"

SAMPLE = """# Sample Policy

Intro paragraph that explains the policy in a few words for everyone reading it today and later on.

## Short one

Tiny.

## Real section

This section has enough words to stand alone as its own chunk because it keeps on talking about
deposits, payments, refunds, rescheduling and the many details people ask about every week.
"""


def test_loader_skips_brand_voice_and_reads_titles():
    docs = load_documents(DOCS)
    assert {d.source for d in docs} == {
        "cancellation-and-rescheduling", "delivery-timelines", "usage-and-print-rights",
        "what-to-wear-and-expect", "deposits-and-payment", "weather-and-backup-plans",
    }
    assert all(d.title and not d.title.startswith("#") for d in docs)


def test_split_sections_uses_title_for_intro():
    secs = split_sections(Document("s", "Sample Policy", SAMPLE))
    assert [h for h, _ in secs] == ["Sample Policy", "Short one", "Real section"]


def test_small_sections_merge_forward_and_ids_carry_source():
    chunks = chunk_document(Document("sample", "Sample Policy", SAMPLE), min_tokens=10)
    assert [c.heading for c in chunks] == ["Sample Policy", "Short one / Real section"]
    assert [c.chunk_id for c in chunks] == ["sample#sample-policy#0", "sample#short-one-real-section#0"]
    assert "Tiny." in chunks[1].text


def test_long_section_is_windowed_with_overlap_and_bounded():
    paras = [f"Paragraph {i} " + " ".join(f"word{i}_{j}" for j in range(40)) + "." for i in range(12)]
    doc = Document("long", "Long", "# Long\n\n## Big\n\n" + "\n\n".join(paras))
    chunks = chunk_document(doc, target_tokens=150, max_tokens=180, overlap_tokens=60)
    assert len(chunks) > 3
    assert all(estimate_tokens(c.text) <= 180 for c in chunks)
    assert [c.chunk_id for c in chunks] == [f"long#big#{i}" for i in range(len(chunks))]
    # overlap: each window starts with the last paragraph of the previous one
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.text.split("\n\n")[-1] == nxt.text.split("\n\n")[0]
    # nothing lost
    joined = "\n\n".join(c.text for c in chunks)
    assert all(p in joined for p in paras)


def test_real_corpus_chunks_are_unique_complete_and_sized():
    docs = load_documents(DOCS)
    chunks = chunk_documents(docs)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert all(estimate_tokens(c.text) <= 380 for c in chunks)
    for d in docs:  # every paragraph of every doc lands in some chunk
        body = "\n\n".join(c.text for c in chunks if c.source == d.source)
        for para in d.text.split("\n\n"):
            para = para.strip()
            if para and not para.startswith("#"):
                assert para in body, para[:60]


def test_chunking_is_deterministic():
    docs = load_documents(DOCS)
    assert chunk_documents(docs) == chunk_documents(docs)
