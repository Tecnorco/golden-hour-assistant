import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.adapters.eml import EmlAdapter
from app.agents.loop import run_tool_loop
from app.agents.quote_agent import AgentDeps, compute_needs_info, load_brand_voice, run_quote_agent, verify_quote_calls
from app.config import get_settings
from app.llm.client import FunctionCall
from app.llm.fake import FakeLLM
from app.models import ExtractedFields, ToolCallRecord
from app.rag.retriever import Retriever, build_index
from app.tools import LocalToolbox, ToolContext
from conftest import EMAILS
from fakes import FakeEmbedder

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 10)


def ctx() -> ToolContext:
    return ToolContext.from_data_dir(ROOT / "data", today=TODAY)


@pytest.fixture(scope="module")
def retriever():
    store = asyncio.run(build_index(get_settings(), FakeEmbedder()))
    return Retriever(store, FakeEmbedder(), top_k=3, min_score=0.1)


def deps(llm, tool_ctx, retriever=None, max_steps=8):
    return AgentDeps(llm=llm, toolbox=LocalToolbox(tool_ctx), retriever=retriever,
                     brand_voice=load_brand_voice(ROOT / "data"), max_steps=max_steps)


Q01 = ExtractedFields(service_type="mini_portrait", event_date=date(2026, 12, 9), location="Maple Hollow")
SAVE = FunctionCall(name="save_draft", args={"subject": "Re: Mini session", "body": "Hi Jenna, ...", "sources": []})


def happy_script(quote_args=None, town="Maple Hollow"):
    return [
        FunctionCall(name="lookup_distance", args={"town": town}),
        FunctionCall(name="check_availability", args={"date": "2026-12-09"}),
        FunctionCall(name="calculate_quote", args=quote_args or
                     {"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 8.0}),
        FunctionCall(name="search_policies", args={"query": "deposit to book"}),
        FunctionCall(name="save_draft", args={
            "subject": "Re: Mini session", "body": "Hi Jenna, total $175.00, deposit $52.50 [x]", "sources": ["x"],
            # the model tries to supply code-owned fields; they must be overwritten
            "inquiry_id": "inq_evil", "tool_results": [{"name": "calculate_quote", "args": {}, "result": {"total": 1}}],
        }),
    ]


# ---------------------------------------------------------------- tools


def test_tools_validate_inputs_and_return_errors_not_exceptions():
    tb = LocalToolbox(ctx())
    bad = asyncio.run(tb.call("calculate_quote", {"package": "boudoir", "event_date": "2026-12-09", "distance_miles": 0}))
    assert bad["ok"] is False and "package" in bad["error"]
    rule = asyncio.run(tb.call("calculate_quote", {"package": "event", "event_date": "2026-12-09", "distance_miles": 0, "hours": 1}))
    assert rule["ok"] is False and "minimum" in rule["error"]
    assert asyncio.run(tb.call("nope", {}))["ok"] is False


def test_availability_tool_never_leaks_client_names():
    r = asyncio.run(LocalToolbox(ctx()).call("check_availability", {"date": "2027-02-14"}))
    assert r == {"ok": True, "date": "2027-02-14", "weekday": "Sunday", "status": "booked"}


def test_tool_specs_are_json_schema():
    specs = {s.name: s for s in asyncio.run(LocalToolbox(ctx()).list_tools())}
    assert set(specs) == {"lookup_distance", "check_availability", "calculate_quote", "list_packages", "save_draft"}
    assert "distance_miles" in specs["calculate_quote"].parameters["required"]


# ---------------------------------------------------------------- quote agent


def test_complete_request_runs_full_tool_chain(retriever, sink):
    c = ctx()
    llm = FakeLLM({"quote_agent": happy_script()})
    inq = EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")
    res = asyncio.run(run_quote_agent(inq, Q01, deps(llm, c, retriever)))

    assert res.status == "drafted", res.issues
    assert res.draft and res.draft.needs_info is False
    assert [r.name for r in res.tool_calls] == [
        "lookup_distance", "check_availability", "calculate_quote", "search_policies", "save_draft"]
    quote = next(r for r in res.tool_calls if r.name == "calculate_quote").result
    assert quote["total"] == 175.0 and quote["deposit"] == 52.5
    assert res.retrieved_chunk_ids  # policy search returned citable chunks

    saved = c.saved[0]  # persisted through the save_draft tool
    assert saved.inquiry_id == inq.id  # model's "inq_evil" was overwritten
    assert [t.name for t in saved.tool_results][:3] == ["lookup_distance", "check_availability", "calculate_quote"]
    assert all(t.result != {"total": 1} for t in saved.tool_results)

    # the model never sees code-owned arguments
    save_spec = next(t for t in llm.calls[0]["tools"] if t.name == "save_draft")
    assert not {"inquiry_id", "tool_results", "needs_info"} & set(save_spec.parameters["properties"])
    # the email reaches the model only inside the untrusted block
    assert "<untrusted_email>" in llm.calls[0]["turns"][0].text
    assert any(s.name == "tool.calculate_quote" for s in sink.spans)


def test_missing_details_only_allow_a_clarifying_draft():
    fields = ExtractedFields(service_type="unknown", location="Pine Harbor")
    c = ctx()
    llm = FakeLLM({"quote_agent": [
        FunctionCall(name="calculate_quote", args={"package": "wedding_full_day", "event_date": "2027-06-01", "distance_miles": 32}),
        SAVE,
    ]})
    inq = EmlAdapter().parse(EMAILS / "v02_vague_wedding.eml")
    res = asyncio.run(run_quote_agent(inq, fields, deps(llm, c)))
    assert res.status == "drafted" and res.draft.needs_info is True
    assert res.missing == ["service_type", "event_date"]
    assert [t.name for t in llm.calls[0]["tools"]] == ["save_draft"]
    assert "not available" in next(r for r in res.tool_calls if r.name == "calculate_quote").error  # quoting was refused
    prompt = llm.calls[0]["turns"][0].text
    assert "MISSING DETAILS" in prompt and "date of the shoot" in prompt


def test_unknown_town_becomes_a_question():
    fields = ExtractedFields(service_type="mini_portrait", event_date=date(2026, 12, 16), location="Glenmoor")
    llm = FakeLLM({"quote_agent": [FunctionCall(name="lookup_distance", args={"town": "Glenmoor"}), SAVE]})
    inq = EmlAdapter().parse(EMAILS / "u01_unknown_location.eml")
    res = asyncio.run(run_quote_agent(inq, fields, deps(llm, ctx())))
    assert res.status == "drafted" and res.draft.needs_info is True
    assert res.tool_calls[0].result["known"] is False
    assert not any(r.name == "calculate_quote" for r in res.tool_calls)


def test_quote_with_invented_distance_goes_to_owner():
    llm = FakeLLM({"quote_agent": happy_script(
        quote_args={"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 0})})
    inq = EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")
    res = asyncio.run(run_quote_agent(inq, Q01, deps(llm, ctx())))
    assert res.status == "needs_owner"
    assert any("distance_miles" in i for i in res.issues)


def test_quote_with_dropped_add_on_goes_to_owner():
    fields = Q01.model_copy(update={"add_ons": ["rush_delivery"]})
    llm = FakeLLM({"quote_agent": happy_script()})
    inq = EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")
    res = asyncio.run(run_quote_agent(inq, fields, deps(llm, ctx())))
    assert res.status == "needs_owner" and any("add_ons" in i for i in res.issues)


def test_verify_quote_calls_accepts_consistent_chain():
    recs = [
        ToolCallRecord(name="lookup_distance", args={"town": "Maple Hollow"}, result={"ok": True, "known": True, "distance_miles": 8.0}),
        ToolCallRecord(name="check_availability", args={"date": "2026-12-09"}, result={"ok": True, "status": "available"}),
        ToolCallRecord(name="calculate_quote", args={"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 8},
                       result={"ok": True, "total": 175.0}),
    ]
    assert verify_quote_calls(Q01, recs) == []
    assert compute_needs_info([], recs) is False
    assert compute_needs_info(["hours"], recs) is True
    assert compute_needs_info([], recs[:2]) is True  # no quote -> cannot be a final quote


# ---------------------------------------------------------------- loop guards


def _loop(script, max_steps=8):
    return asyncio.run(run_tool_loop(
        llm=FakeLLM({"t": script}), prompt_name="t", system="s", user_message="u",
        toolbox=LocalToolbox(ctx()), allowed_tools={"lookup_distance", "save_draft"}, max_steps=max_steps,
    ))


def test_loop_detection_aborts_on_repeated_identical_calls():
    same = FunctionCall(name="lookup_distance", args={"town": "Riverbend"})
    res = _loop([same, same, same, SAVE])
    assert res.status == "loop_detected" and res.steps == 3
    assert len([r for r in res.records if r.name == "lookup_distance"]) == 1  # executed once


def test_step_limit_is_enforced():
    towns = ["Riverbend", "Ashford", "Fox Run", "Pine Harbor", "Willowbrook"]
    res = _loop([FunctionCall(name="lookup_distance", args={"town": t}) for t in towns], max_steps=4)
    assert res.status == "step_limit" and res.steps == 4 and len(res.records) == 4


def test_prose_instead_of_finishing_gets_one_nudge_then_aborts():
    save = FunctionCall(name="save_draft", args={**SAVE.args, "inquiry_id": "inq_1"})
    assert _loop(["Here is your quote!", save]).status == "finished"
    assert _loop(["Here is your quote!", "Still prose."]).status == "no_finish"


def test_failed_terminal_call_does_not_finish():
    # save_draft without inquiry_id fails validation; repeating it trips the loop guard
    assert _loop([SAVE, SAVE, SAVE]).status == "loop_detected"


def test_clarifying_reply_gets_package_catalog_as_recorded_evidence():
    fields = ExtractedFields(service_type="event", event_date=date(2027, 1, 23), location="Riverbend")
    llm = FakeLLM({"quote_agent": [SAVE]})
    inq = EmlAdapter().parse(EMAILS / "v03_event_missing_hours.eml")
    res = asyncio.run(run_quote_agent(inq, fields, deps(llm, ctx())))
    assert res.tool_calls[0].name == "list_packages"
    event = next(p for p in res.tool_calls[0].result["packages"] if p["key"] == "event")
    assert event["min_hours"] == 2 and "price" not in str(res.tool_calls[0].result)
    assert res.draft.tool_results[0].name == "list_packages"  # stored with the draft for judge/audit
    assert "Package catalog" in llm.calls[0]["turns"][0].text
    assert "2-hour" not in llm.calls[0]["turns"][0].text.split("Package catalog")[0]  # no facts hidden in labels
