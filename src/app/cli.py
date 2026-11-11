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


def cmd_quote(args: argparse.Namespace) -> int:
    """Phase 3 harness: extract -> quote agent, with in-process tools."""
    from app.agents.extractor import extract_fields
    from app.agents.quote_agent import AgentDeps, load_brand_voice, run_quote_agent
    from app.llm.client import GeminiLLM
    from app.rag import load_retriever
    from app.tools import LocalToolbox, ToolContext

    settings = get_settings()
    store = _store()
    llm = GeminiLLM(settings)
    deps = AgentDeps(
        llm=llm,
        toolbox=LocalToolbox(ToolContext.from_data_dir(settings.data_dir, save_draft_fn=store.add_draft)),
        retriever=load_retriever(settings, llm),
        brand_voice=load_brand_voice(settings.data_dir),
        max_steps=settings.agent_max_steps,
    )
    adapter = EmlAdapter()

    async def run() -> None:
        for path in _emails(args.paths):
            inq = adapter.parse(path)
            store.add_inquiry(inq)
            with tracing.trace(inq.id, name="quote", input={"file": path.name}) as root:
                fields = await extract_fields(inq, llm)
                res = await run_quote_agent(inq, fields, deps)
                root.output = res.status
            quote = next((r.result for r in reversed(res.tool_calls) if r.name == "calculate_quote" and r.error is None), None)
            print(f"\n=== {path.name}  [{inq.id}]  status={res.status} steps={res.steps} "
                  f"needs_info={res.draft.needs_info if res.draft else '-'} "
                  f"total={quote['total'] if quote else '-'} {res.reason} {res.issues or ''}")
            print("tools:", " -> ".join(r.name + ("!" if r.error else "") for r in res.tool_calls))
            if res.draft and not args.quiet:
                print(f"Subject: {res.draft.subject}\n{res.draft.body}\nsources: {res.draft.sources}")

    asyncio.run(run())
    return 0


async def build_pipeline_and_run(paths: list[Path], verbose: bool = False) -> list:
    """Run the full pipeline over .eml files. Business tools are reached over MCP."""
    from app.agents.base import AgentDeps, load_brand_voice
    from app.guardrails.input import make_input_guard
    from app.guardrails.output import output_guard
    from app.guardrails.repair import make_repairer
    from app.llm.client import GeminiLLM
    from app.mcp_server.client import MCPToolbox, server_params
    from app.orchestrator import Pipeline
    from app.rag import load_retriever

    settings = get_settings()
    store = _store()
    llm = GeminiLLM(settings)
    retriever = load_retriever(settings, llm)
    adapter = EmlAdapter()
    outcomes = []
    async with MCPToolbox(server_params(settings.data_dir, settings.db_path)) as toolbox:
        deps = AgentDeps(llm=llm, toolbox=toolbox, retriever=retriever,
                         brand_voice=load_brand_voice(settings.data_dir), max_steps=settings.agent_max_steps)
        pipeline = Pipeline(
            store=store, deps=deps, settings=settings,
            input_guard=make_input_guard(llm, settings),
            output_guard=output_guard,
            repair=make_repairer(deps),
        )
        for path in paths:
            out = await pipeline.process(adapter.parse(path))
            outcomes.append((path, out))
            label = f"{out.route.label}({out.route.confidence:.2f})" if out.route else "-"
            flags = ("needs_info " if out.needs_info else "") + ("flag_owner" if out.flag_owner else "")
            print(f"{path.name:34s} {out.inquiry_id}  {label:24s} {out.status.value:17s} {flags:22s} {out.reason[:70]}")
            if verbose and out.draft:
                print(f"    Subject: {out.draft.subject}\n    " + out.draft.body.replace("\n", "\n    "))
    return outcomes


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

    p = sub.add_parser("queue", help="list inquiries (optionally by status)")
    p.add_argument("--status", choices=[s.value for s in InquiryStatus])
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("decide", help="approve / edit-and-approve / reject a draft (CLI approval)")
    p.add_argument("inquiry_id")
    p.add_argument("action", choices=["approve", "reject"])
    p.add_argument("--edited-file", help="send this text instead of the draft (edit and approve)")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("quote", help="run extraction + quote agent on .eml files (calls Gemini)")
    p.add_argument("paths", nargs="*")
    p.add_argument("-q", "--quiet", action="store_true", help="do not print draft bodies")
    p.set_defaults(func=cmd_quote)

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
