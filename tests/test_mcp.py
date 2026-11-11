"""Spawns the real MCP server over stdio. No API key needed."""

import asyncio
from datetime import date
from pathlib import Path

from mcp.client.stdio import get_default_environment

from app.adapters.eml import EmlAdapter
from app.agents.quote_agent import AgentDeps, load_brand_voice, run_quote_agent
from app.llm.client import to_function_declarations
from app.llm.fake import FakeLLM
from app.mcp_server.client import MCPToolbox, server_params
from app.models import ExtractedFields
from app.store import Store
from conftest import EMAILS
from test_quote_agent import happy_script

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


async def _with_toolbox(fn, db=None):
    async with MCPToolbox(server_params(DATA, db)) as tb:
        return await fn(tb)


def test_lists_tools_and_converts_to_gemini_declarations():
    specs = asyncio.run(_with_toolbox(lambda tb: tb.list_tools()))
    assert {s.name for s in specs} == {"lookup_distance", "check_availability", "calculate_quote", "save_draft"}
    quote = next(s for s in specs if s.name == "calculate_quote")
    assert set(quote.parameters["required"]) == {"package", "event_date", "distance_miles"}
    assert "enum" in str(quote.parameters["properties"]["package"])
    decls = to_function_declarations(specs)[0].function_declarations
    assert len(decls) == 4 and all(d.parameters_json_schema for d in decls)


def test_quote_over_mcp_matches_golden_total():
    async def go(tb):
        return await tb.call("calculate_quote", {
            "package": "event", "event_date": "2027-02-14", "distance_miles": 0, "hours": 3,
            "add_ons": ["second_photographer"]})
    r = asyncio.run(_with_toolbox(go))
    assert r["ok"] and r["total"] == 1142.5 and r["deposit"] == 342.75


def test_errors_come_back_as_data_not_crashes():
    async def go(tb):
        return [
            await tb.call("calculate_quote", {"package": "mini_portrait", "event_date": "2026-12-09",
                                              "distance_miles": 0, "add_ons": ["second_photographer"]}),
            await tb.call("check_availability", {"date": "not-a-date"}),
            await tb.call("calculate_quote", {"package": "boudoir", "event_date": "2026-12-09", "distance_miles": 0}),
        ]
    rule, bad_date, bad_enum = asyncio.run(_with_toolbox(go))
    assert rule == {"ok": False, "error": "add-on 'second_photographer' is only available for: event, wedding_half_day, wedding_full_day"}
    assert bad_date["ok"] is False and "date" in bad_date["error"]
    assert bad_enum["ok"] is False


def test_save_draft_persists_to_sqlite(tmp_path):
    db = tmp_path / "a.db"
    r = asyncio.run(_with_toolbox(lambda tb: tb.call("save_draft", {
        "inquiry_id": "inq_x", "subject": "Re: hi", "body": "Hi!", "sources": ["a#b#0"]}), db=db))
    assert r == {"ok": True, "saved": True, "inquiry_id": "inq_x"}
    draft = Store(db).latest_draft("inq_x")
    assert draft and draft.body == "Hi!" and draft.sources == ["a#b#0"]


def test_server_process_never_receives_the_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.must-not-leak")
    params = server_params(DATA, None)
    assert params.env is None  # -> SDK passes only its allowlist
    assert "GEMINI_API_KEY" not in get_default_environment()
    src = (ROOT / "src" / "app" / "mcp_server" / "server.py").read_text()
    assert "get_settings" not in src and "dotenv" not in src  # server never loads .env


def test_quote_agent_runs_through_mcp(tmp_path):
    db = tmp_path / "a.db"
    fields = ExtractedFields(service_type="mini_portrait", event_date=date(2026, 12, 9), location="Maple Hollow")
    inq = EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")
    llm = FakeLLM({"quote_agent": happy_script()})

    async def go(tb):
        deps = AgentDeps(llm=llm, toolbox=tb, retriever=None, brand_voice=load_brand_voice(DATA))
        return await run_quote_agent(inq, fields, deps)

    res = asyncio.run(_with_toolbox(go, db=db))
    assert res.status == "drafted", res.issues
    quote = next(r for r in res.tool_calls if r.name == "calculate_quote").result
    assert quote["total"] == 175.0
    stored = Store(db).latest_draft(inq.id)  # persisted by the MCP server process
    assert stored and stored.inquiry_id == inq.id and stored.tool_results
