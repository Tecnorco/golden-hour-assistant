"""Heading-aware markdown chunker.

Strategy:
1. Split each document into sections at '#'/'##'/'###' headings. A chunk never
   spans two headings unless a section is tiny, so every citation points to one
   meaningful heading.
2. Sections under `min_tokens` are merged into the next section of the same
   document (heading becomes "A / B") so we do not index one-line fragments.
3. Sections over `max_tokens` are split on paragraph boundaries into windows of
   about `target_tokens`, carrying ~`overlap_tokens` of trailing text into the next
   window so a fact split across a boundary is still retrievable.

Token counts are estimated as words x 1.3 (no tokenizer dependency); this is
close enough for sizing chunks.
"""

from __future__ import annotations

import re

from app.models import Chunk
from app.rag.loader import Document

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


def estimate_tokens(text: str) -> int:
    return int(len(text.split()) * 1.3)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def split_sections(doc: Document) -> list[tuple[str, str]]:
    """Returns (heading, body) pairs. Text before the first '##' belongs to the title."""
    sections: list[tuple[str, list[str]]] = [(doc.title, [])]
    for line in doc.text.splitlines():
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            if level == 1:
                continue  # the document title; already used as the intro heading
            sections.append((m.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    out = [(h, "\n".join(lines).strip()) for h, lines in sections]
    return [(h, b) for h, b in out if b]


def _merge_small(sections: list[tuple[str, str]], min_tokens: int) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    carry: tuple[str, str] | None = None
    for heading, body in sections:
        if carry:
            heading, body = f"{carry[0]} / {heading}", f"{carry[1]}\n\n{body}"
            carry = None
        if estimate_tokens(body) < min_tokens:
            carry = (heading, body)
        else:
            merged.append((heading, body))
    if carry:  # small trailing section: attach to the previous one, or keep alone
        if merged:
            h, b = merged.pop()
            merged.append((f"{h} / {carry[0]}", f"{b}\n\n{carry[1]}"))
        else:
            merged.append(carry)
    return merged


def _windows(body: str, target: int, overlap: int) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    # Very long single paragraphs are split on sentences so windows stay bounded.
    units: list[str] = []
    for p in paras:
        if estimate_tokens(p) > target:
            units.extend(s for s in re.split(r"(?<=[.!?])\s+", p) if s)
        else:
            units.append(p)

    windows: list[list[str]] = [[]]
    for u in units:
        if windows[-1] and estimate_tokens(" ".join(windows[-1] + [u])) > target:
            # seed the next window with trailing units up to `overlap` tokens
            tail: list[str] = []
            for prev in reversed(windows[-1]):
                if estimate_tokens(" ".join([prev] + tail)) > overlap:
                    break
                tail.insert(0, prev)
            windows.append(tail)
        windows[-1].append(u)
    return ["\n\n".join(w) for w in windows if w]


def chunk_document(
    doc: Document,
    target_tokens: int = 300,
    max_tokens: int = 380,
    overlap_tokens: int = 50,
    min_tokens: int = 40,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, body in _merge_small(split_sections(doc), min_tokens):
        parts = [body] if estimate_tokens(body) <= max_tokens else _windows(body, target_tokens, overlap_tokens)
        for i, part in enumerate(parts):
            chunks.append(
                Chunk(chunk_id=f"{doc.source}#{slugify(heading)}#{i}", source=doc.source, heading=heading, text=part)
            )
    return chunks


def chunk_documents(docs: list[Document], **kwargs: int) -> list[Chunk]:
    out: list[Chunk] = []
    for d in docs:
        out.extend(chunk_document(d, **kwargs))
    return out
