"""`make doctor`: check every moving part, in dependency order, with a fix hint.

Run it after a fresh clone or a deployment. Each check is independent and
reports PASS / FAIL / SKIP; the exit code is non-zero if anything failed.
The API key is never printed, only whether it is set.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

PROMPT_NAMES = ["router", "extractor", "quote_agent", "faq_agent", "other_agent", "injection_classifier", "repair", "judge"]
EXPECTED_TOOLS = {"lookup_distance", "check_availability", "calculate_quote", "list_packages", "save_draft"}


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str
    fix: str = ""


class Skip(Exception):
    pass


async def _run(name: str, fn: Callable[[], Awaitable[str]], fix: str) -> Check:
    t0 = time.perf_counter()
    try:
        detail = await fn()
        return Check(name, "PASS", f"{detail} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
    except Skip as exc:
        return Check(name, "SKIP", str(exc))
    except Exception as exc:  # noqa: BLE001 - the doctor's job is to report any failure
        return Check(name, "FAIL", f"{type(exc).__name__}: {exc}"[:300], fix)


async def run_checks(settings: Settings, offline: bool = False, quick: bool = False) -> list[Check]:
    s = settings
    checks: list[Check] = []

    async def python() -> str:
        if sys.version_info < (3, 11):
            raise RuntimeError(f"Python {sys.version.split()[0]} < 3.11")
        return f"Python {sys.version.split()[0]}"

    async def api_key() -> str:
        if s.api_key is None or not s.api_key.get_secret_value().strip():
            raise RuntimeError("GEMINI_API_KEY is not set")
        return "GEMINI_API_KEY is set (value not shown)"

    async def data() -> str:
        from app.pricing.engine import load_calendar, load_price_list

        pl = load_price_list(s.data_dir / "pricing.yaml")
        cal = load_calendar(s.data_dir / "calendar.json")
        docs = len(list((s.data_dir / "docs").glob("*.md")))
        emails = len(list((s.data_dir / "emails").glob("*.eml")))
        if docs < 7 or emails < 1:
            raise RuntimeError(f"expected 7 docs and some emails, found {docs} docs / {emails} emails")
        return f"{len(pl.packages)} packages, {len(pl.towns)} towns, {len(cal.booked)} booked dates, {docs} docs, {emails} emails"

    async def prompts() -> str:
        from app.llm.prompts import load_prompt

        load_prompt.cache_clear()
        return ", ".join(f"{p} v{load_prompt(p).version}" for p in PROMPT_NAMES)

    async def sqlite() -> str:
        from app.store import Store

        store = Store(s.db_path)
        return f"{s.db_path} writable, {len(store.list_inquiries())} inquiries"

    async def index() -> str:
        from app.rag.store import VectorStore

        vs = VectorStore.load(s.index_dir)
        if vs.meta.get("model") != s.embedding_model or vs.meta.get("dim") != s.embedding_dim:
            raise RuntimeError(f"index built with {vs.meta.get('model')}/{vs.meta.get('dim')}, "
                               f"config wants {s.embedding_model}/{s.embedding_dim}")
        return f"{len(vs.chunks)} chunks, {vs.meta.get('model')} dim {vs.meta.get('dim')}"

    async def mcp() -> str:
        from app.mcp_server.client import MCPToolbox, server_params

        async with MCPToolbox(server_params(s.data_dir, None)) as tb:
            names = {t.name for t in await tb.list_tools()}
            if names != EXPECTED_TOOLS:
                raise RuntimeError(f"tools {sorted(names)} != {sorted(EXPECTED_TOOLS)}")
            r = await tb.call("calculate_quote", {"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 8})
            if r.get("total") != 175.0:
                raise RuntimeError(f"calculate_quote returned {r}")
        return f"server started, {len(names)} tools, calculate_quote OK"

    def _need_api() -> None:
        if offline:
            raise Skip("skipped (--offline)")
        if s.api_key is None:
            raise Skip("skipped (no API key)")

    async def chat() -> str:
        _need_api()
        from pydantic import BaseModel

        from app.llm.client import GeminiLLM

        class Pong(BaseModel):
            ok: bool

        await GeminiLLM(s).generate(prompt_name="doctor", system="Reply with JSON.", turns='Return {"ok": true}.',
                                        schema=Pong, temperature=0)
        return f"{s.chat_model} answered with valid structured output"

    async def embed() -> str:
        _need_api()
        from app.llm.client import GeminiLLM

        v = await GeminiLLM(s).embed(["deposit policy"], task_type="RETRIEVAL_QUERY")
        if len(v[0]) != s.embedding_dim:
            raise RuntimeError(f"got {len(v[0])} dims, expected {s.embedding_dim}")
        return f"{s.embedding_model} returned {len(v[0])} dims"

    async def end_to_end() -> str:
        _need_api()
        if quick:
            raise Skip("skipped (--quick)")
        from app.adapters.eml import EmlAdapter
        from app.agents.base import AgentDeps, load_brand_voice
        from app.guardrails.input import make_input_guard
        from app.guardrails.output import output_guard
        from app.guardrails.repair import make_repairer
        from app.llm.client import GeminiLLM
        from app.mcp_server.client import MCPToolbox, server_params
        from app.observability import tracing
        from app.orchestrator import Pipeline
        from app.rag import load_retriever
        from app.store import Store

        db = s.index_dir.parent / "doctor.db"
        db.unlink(missing_ok=True)
        store = Store(db)
        tracing.set_sink(store)
        llm = GeminiLLM(s)
        async with MCPToolbox(server_params(s.data_dir, db)) as tb:
            deps = AgentDeps(llm=llm, toolbox=tb, retriever=load_retriever(s, llm),
                             brand_voice=load_brand_voice(s.data_dir), max_steps=s.agent_max_steps)
            p = Pipeline(store=store, deps=deps, settings=s, input_guard=make_input_guard(llm, s),
                         output_guard=output_guard, repair=make_repairer(deps))
            out = await p.process(EmlAdapter().parse(s.data_dir / "emails" / "q01_mini_weekday.eml"))
        if out.status.value != "PENDING_APPROVAL" or not out.draft or "$175.00" not in out.draft.body:
            raise RuntimeError(f"status {out.status.value}: {out.reason or 'draft missing $175.00'} "
                               f"(inspect: python -m app.cli trace {out.inquiry_id} --db {db})")
        return f"q01 -> PENDING_APPROVAL with $175.00 quote (trace: {out.inquiry_id} in {db.name})"

    plan: list[tuple[str, Callable[[], Awaitable[str]], str]] = [
        ("python", python, "Install Python 3.11+ and re-run `make setup`."),
        ("api key", api_key, "Copy .env.example to .env and set GEMINI_API_KEY (or set it in your host's secret settings)."),
        ("demo data", data, "Restore data/ from git: `git checkout -- data`."),
        ("prompts", prompts, "Restore prompts/ from git: `git checkout -- prompts`."),
        ("sqlite", sqlite, "Check that the var/ directory (or GHA_DB_PATH) is writable and the disk is not full."),
        ("vector index", index, "Build it: `make index`."),
        ("mcp server", mcp, "Run `make mcp-demo` to see the server's error output; check that `make setup` succeeded."),
        ("gemini chat", chat, "Check the key, network access to generativelanguage.googleapis.com and GEMINI_CHAT_MODEL."),
        ("gemini embeddings", embed, "Check GEMINI_EMBEDDING_MODEL / GHA_EMBEDDING_DIM, then `make index`."),
        ("end to end", end_to_end, "Run the trace command shown above to see which step failed."),
    ]
    for name, fn, fix in plan:
        checks.append(await _run(name, fn, fix))
    return checks


def print_checks(checks: list[Check]) -> int:
    icons = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ "}
    for c in checks:
        print(f"{icons[c.status]} {c.name:18s} {c.detail}")
        if c.status == "FAIL" and c.fix:
            print(f"   {'':18s} fix: {c.fix}")
    failed = [c for c in checks if c.status == "FAIL"]
    print(f"\n{len(checks) - len(failed)} ok, {len(failed)} failed" + (" - see fixes above." if failed else ". All good."))
    return 1 if failed else 0


def main(offline: bool = False, quick: bool = False) -> int:
    return print_checks(asyncio.run(run_checks(get_settings(), offline=offline, quick=quick)))

