"""Command line entry point: python -m app.cli <command>."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.adapters.eml import EmlAdapter
from app.config import get_settings
from app.models import new_id, utcnow
from app.observability import tracing
from app.store import Store


def _store() -> Store:
    store = Store(get_settings().db_path)
    tracing.set_sink(store)  # every span from here on is persisted
    return store


def _emails(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths or [str(get_settings().data_dir / "emails")]:
        path = Path(p)
        out.extend(sorted(path.glob("*.eml")) if path.is_dir() else [path])
    return out


def cmd_parse(args: argparse.Namespace) -> int:
    adapter = EmlAdapter()
    for path in _emails(args.paths):
        inq = adapter.parse(path)
        print(json.dumps(inq.model_dump(mode="json", exclude={"body"}), indent=2))
        print(inq.body[:400], "\n" + "-" * 60)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from app.agents.extractor import extract_fields
    from app.llm.client import GeminiLLM

    store = _store()
    llm = GeminiLLM()
    adapter = EmlAdapter()

    async def run() -> None:
        for path in _emails(args.paths):
            inq = adapter.parse(path)
            store.add_inquiry(inq)
            with tracing.trace(inq.id, name="extract", input={"file": path.name}):
                fields = await extract_fields(inq, llm)
            print(f"{path.name:36s} {inq.id}  {fields.model_dump_json()}")

    asyncio.run(run())
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from app.llm.client import GeminiLLM
    from app.rag import make_embedder
    from app.rag.retriever import build_index

    settings = get_settings()
    _store()
    embedder = make_embedder(settings, GeminiLLM(settings))

    async def run() -> None:
        with tracing.trace(new_id("index"), name="index"):
            store = await build_index(settings, embedder)
        store.save(settings.index_dir)
        print(f"Indexed {store.meta['chunks']} chunks from {store.meta['documents']} documents "
              f"-> {settings.index_dir} ({embedder.api_calls} embedding API call(s), rest from cache)")

    asyncio.run(run())
    return 0


def cmd_retrieve(args: argparse.Namespace) -> int:
    from app.llm.client import GeminiLLM
    from app.rag import load_retriever

    settings = get_settings()
    _store()
    retriever = load_retriever(settings, GeminiLLM(settings))

    async def run() -> None:
        with tracing.trace(new_id("retrieve"), name="retrieve"):
            hits = await retriever.retrieve(args.query, k=args.k, min_score=-1.0)
        for h in hits:
            mark = " " if h.score >= retriever.min_score else "x"  # x = below threshold, would not be used
            print(f"{mark} {h.score:.3f}  {h.chunk_id}")

    asyncio.run(run())
    return 0


def cmd_eval_retrieval(args: argparse.Namespace) -> int:
    from app.evaluation.retrieval import load_pairs, run_retrieval_eval, to_markdown
    from app.llm.client import GeminiLLM
    from app.rag import load_retriever

    settings = get_settings()
    _store()
    retriever = load_retriever(settings, GeminiLLM(settings))
    data = load_pairs(settings.data_dir.parent / "evals" / "retrieval_pairs.yaml")

    async def run() -> None:
        with tracing.trace(new_id("reval"), name="eval_retrieval"):
            report = await run_retrieval_eval(retriever, data, k=args.k)
        md = to_markdown(report)
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        path = settings.reports_dir / f"retrieval-{utcnow().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(md, encoding="utf-8")
        (settings.reports_dir / f"{path.stem}.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
        print(md)
        print(f"Saved {path}")

    asyncio.run(run())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Golden Hour Assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="parse .eml files into Inquiry objects (no LLM)")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("extract", help="run field extraction on .eml files (calls Gemini)")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("index", help="chunk + embed data/docs into the vector index")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("retrieve", help="show top chunks and scores for a query")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.set_defaults(func=cmd_retrieve)

    p = sub.add_parser("eval-retrieval", help="hit rate @k on evals/retrieval_pairs.yaml")
    p.add_argument("-k", type=int, default=4)
    p.set_defaults(func=cmd_eval_retrieval)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
