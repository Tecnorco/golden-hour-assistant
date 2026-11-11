"""Retrieval eval: hit rate @k, MRR, and threshold calibration on off-topic queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from app.rag.retriever import Retriever


class PairResult(BaseModel):
    question: str
    source: str
    rank: int | None  # 1-based rank of the first correct chunk in the top k, None = miss
    top_chunk: str
    top_score: float
    hit_score: float | None


class RetrievalReport(BaseModel):
    k: int
    min_score: float
    n_pairs: int
    hit_rate: float  # correct chunk in top k (ranking only)
    answerable_rate: float  # correct chunk in top k AND above the threshold
    mrr: float
    negatives_false_hit_rate: float  # off-topic queries that still returned a chunk
    lowest_hit_score: float | None
    highest_negative_score: float | None
    pairs: list[PairResult]
    negatives: list[dict[str, Any]]
    meta: dict[str, Any]


def load_pairs(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


async def run_retrieval_eval(retriever: Retriever, data: dict[str, Any], k: int) -> RetrievalReport:
    results: list[PairResult] = []
    for p in data["pairs"]:
        # min_score=-1 -> pure ranking; the threshold is applied separately below.
        chunks = await retriever.retrieve(p["q"], k=k, min_score=-1.0)
        rank = next(
            (i + 1 for i, c in enumerate(chunks) if c.source == p["source"] and p["phrase"] in c.text), None
        )
        results.append(
            PairResult(
                question=p["q"],
                source=p["source"],
                rank=rank,
                top_chunk=chunks[0].chunk_id if chunks else "",
                top_score=chunks[0].score if chunks else 0.0,
                hit_score=chunks[rank - 1].score if rank else None,
            )
        )
    negatives = []
    for q in data.get("negatives", []):
        chunks = await retriever.retrieve(q, k=1, min_score=-1.0)
        top = chunks[0] if chunks else None
        negatives.append({"question": q, "top_chunk": top.chunk_id if top else "", "top_score": top.score if top else 0.0})

    n = len(results) or 1
    hit_scores = [r.hit_score for r in results if r.hit_score is not None]
    neg_scores = [x["top_score"] for x in negatives]
    return RetrievalReport(
        k=k,
        min_score=retriever.min_score,
        n_pairs=len(results),
        hit_rate=round(sum(r.rank is not None for r in results) / n, 3),
        answerable_rate=round(sum(r.hit_score is not None and r.hit_score >= retriever.min_score for r in results) / n, 3),
        mrr=round(sum(1 / r.rank for r in results if r.rank) / n, 3),
        negatives_false_hit_rate=round(sum(s >= retriever.min_score for s in neg_scores) / (len(neg_scores) or 1), 3),
        lowest_hit_score=min(hit_scores) if hit_scores else None,
        highest_negative_score=max(neg_scores) if neg_scores else None,
        pairs=results,
        negatives=negatives,
        meta=retriever.store.meta,
    )


def to_markdown(r: RetrievalReport) -> str:
    lines = [
        "# Retrieval eval",
        "",
        f"Index: {r.meta.get('chunks')} chunks from {r.meta.get('documents')} docs, model `{r.meta.get('model')}` "
        f"dim {r.meta.get('dim')}, chunk target {r.meta.get('chunk_target_tokens')} / min {r.meta.get('chunk_min_tokens')} tokens.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Hit rate @{r.k} | {r.hit_rate:.0%} ({r.n_pairs} questions) |",
        f"| MRR @{r.k} | {r.mrr:.3f} |",
        f"| Answerable (hit and score >= {r.min_score}) | {r.answerable_rate:.0%} |",
        f"| Off-topic queries above threshold | {r.negatives_false_hit_rate:.0%} |",
        f"| Lowest correct-chunk score | {r.lowest_hit_score} |",
        f"| Highest off-topic score | {r.highest_negative_score} |",
        "",
        "## Questions",
        "",
        "| # | Question | Rank | Score | Top chunk |",
        "|---|---|---|---|---|",
    ]
    for i, p in enumerate(r.pairs, 1):
        rank = p.rank if p.rank else "**miss**"
        lines.append(f"| {i} | {p.question} | {rank} | {p.hit_score if p.hit_score is not None else '-'} | `{p.top_chunk}` |")
    lines += ["", "## Off-topic queries", "", "| Question | Top score | Top chunk |", "|---|---|---|"]
    for x in r.negatives:
        lines.append(f"| {x['question']} | {x['top_score']} | `{x['top_chunk']}` |")
    return "\n".join(lines) + "\n"
