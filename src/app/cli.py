"""Command line entry point: python -m app.cli <command>."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.adapters.eml import EmlAdapter
from app.config import get_settings
from app.models import InquiryStatus, new_id, utcnow
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


def cmd_run(args: argparse.Namespace) -> int:
    asyncio.run(build_pipeline_and_run(_emails(args.paths), verbose=args.verbose))
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    store = _store()
    rows = store.list_inquiries(args.status)
    for r in rows:
        inq = store.get_inquiry(r["id"])
        print(f"{r['id']}  {r['status']:17s} {inq.subject[:50] if inq else '':50s} {(r['owner_note'] or '')[:60]}")
    print(f"{len(rows)} item(s)")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """CLI approval loop (fallback for the Streamlit screen)."""
    from app.models import Decision
    from app.orchestrator import record_decision, send_reply

    settings = get_settings()
    store = _store()
    draft = store.latest_draft(args.inquiry_id)
    if draft is not None:
        print(f"Subject: {draft.subject}\n\n{draft.body}\n")
    edited = Path(args.edited_file).read_text(encoding="utf-8") if args.edited_file else None
    action = "edit_approve" if edited else args.action
    record_decision(store, Decision(inquiry_id=args.inquiry_id, action=action, edited_body=edited, note=args.note or ""))
    if action != "reject":
        path = send_reply(store, EmlAdapter(outbox_dir=settings.outbox_dir), args.inquiry_id)
        print(f"Approved and written to {path}")
    else:
        print("Rejected. Nothing sent.")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from app.evaluation.reporting import compare, latest_reports, load_report, to_markdown

    settings = get_settings()
    reports = settings.reports_dir
    reports.mkdir(parents=True, exist_ok=True)

    if args.compare is not None:
        paths = [Path(p) for p in args.compare] if args.compare else latest_reports(reports, 2)
        if len(paths) != 2:
            print("Need two eval runs to compare (run `make eval` twice, or pass two report .json paths).")
            return 1
        md = compare(load_report(paths[0]), load_report(paths[1]))
        out = reports / f"compare-{paths[0].stem}-vs-{paths[1].stem}.md"
        out.write_text(md, encoding="utf-8")
        print(md)
        print(f"Saved {out}")
        return 0

    from app.evaluation.golden import run_golden_eval
    from app.llm.client import GeminiLLM
    from app.rag import load_retriever

    llm = GeminiLLM(settings)
    retriever = load_retriever(settings, llm)
    previous = latest_reports(reports, 1)
    run_id = utcnow().strftime("%Y%m%d-%H%M%S")
    print(f"Running golden eval {run_id} ...")
    report = asyncio.run(run_golden_eval(
        settings, llm, retriever, settings.data_dir.parent / "evals" / "golden.yaml",
        run_id=run_id, concurrency=args.concurrency, judge=not args.no_judge, only=args.only,
    ))
    md = to_markdown(report)
    (reports / f"eval-{run_id}.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
    (reports / f"eval-{run_id}.md").write_text(md, encoding="utf-8")
    print(md)
    if previous and not args.only:
        cmp = compare(load_report(previous[0]), report)
        (reports / f"compare-{previous[0].stem}-vs-eval-{run_id}.md").write_text(cmp, encoding="utf-8")
        print(cmp)
    print(f"Saved {reports / f'eval-{run_id}.md'}")
    return 0 if all(report.targets_met.values()) else 2


def _store_at(db: str | None) -> Store:
    if db:
        store = Store(db)
        tracing.set_sink(store)
        return store
    return _store()


def cmd_trace(args: argparse.Namespace) -> int:
    from app.observability.report import render_trace

    store = _store_at(args.db)
    spans = store.spans_for_trace(args.inquiry_id)
    render_trace(spans)
    row = store.get_row(args.inquiry_id)
    if row:
        print(f"status: {row['status']}  note: {row['owner_note'] or '-'}")
        for t in store.transitions(args.inquiry_id):
            print(f"  {t['at'][:19]}  {t['from_status'] or '-':17s} -> {t['to_status']:17s} {t['reason'] or ''}")
    return 0 if spans else 1


def cmd_report(args: argparse.Namespace) -> int:
    from app.observability.report import ops_report

    store = _store_at(args.db)
    md = ops_report(store)
    settings = get_settings()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    out = settings.reports_dir / f"ops-{utcnow().strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"Saved {out}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from app.doctor import main as doctor

    return doctor(offline=args.offline, quick=args.quick)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Golden Hour Assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="parse .eml files into Inquiry objects (no LLM)")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("extract", help="run field extraction on .eml files (calls Gemini)")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("run", help="process .eml files through the full pipeline (calls Gemini, tools via MCP)")
    p.add_argument("paths", nargs="*")
    p.add_argument("-v", "--verbose", action="store_true", help="print drafts")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("eval", help="run the golden-set eval (calls Gemini) or --compare two runs")
    p.add_argument("--compare", nargs="*", metavar="REPORT_JSON",
                   help="diff two eval runs (default: the two most recent) instead of running")
    p.add_argument("--no-judge", action="store_true", help="skip the LLM-as-judge groundedness step")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--only", help="only run cases whose file name contains this text")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("trace", help="pretty-print the trace timeline of one inquiry")
    p.add_argument("inquiry_id")
    p.add_argument("--db", help="SQLite file (default: the app DB; eval runs print their own path)")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("report", help="operations report: volume, routes, escalations, decisions, cost, latency")
    p.add_argument("--db")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("doctor", help="check config, data, index, MCP, Gemini and one end-to-end run")
    p.add_argument("--offline", action="store_true", help="skip checks that call the Gemini API")
    p.add_argument("--quick", action="store_true", help="skip the end-to-end pipeline run")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("queue", help="list inquiries (optionally by status)")
    p.add_argument("--status", choices=[s.value for s in InquiryStatus])
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("decide", help="approve / edit-and-approve / reject a draft (CLI approval)")
    p.add_argument("inquiry_id")
    p.add_argument("action", choices=["approve", "reject"])
    p.add_argument("--edited-file", help="send this text instead of the draft (edit and approve)")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_decide)

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
