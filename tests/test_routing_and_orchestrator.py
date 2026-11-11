import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.adapters.eml import EmlAdapter
from app.agents.base import AgentDeps, cited_ids, load_brand_voice
from app.agents.router import needs_owner, route
from app.agents.support_agents import run_faq_agent, run_other_agent
from app.config import get_settings
from app.llm.client import FunctionCall, LLMError
from app.llm.fake import FakeLLM
from app.models import GuardrailVerdict, InquiryStatus as S, RouteDecision
from app.orchestrator import TRANSITIONS, InputCheck, InvalidTransition, OutputCheck, Pipeline, transition
from app.rag.retriever import Retriever, build_index
from app.store import Store
from app.tools import LocalToolbox, ToolContext
from conftest import EMAILS
from test_quote_agent import happy_script

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = EmlAdapter()


@pytest.fixture(scope="module")
def index():
    return asyncio.run(build_index(get_settings(), __import__("fakes").FakeEmbedder()))


def retriever(index, min_score=0.1):
    from fakes import FakeEmbedder
    return Retriever(index, FakeEmbedder(), top_k=3, min_score=min_score)


def deps(llm, ret=None, ctx=None):
    ctx = ctx or ToolContext.from_data_dir(ROOT / "data", today=date(2026, 11, 10))
    return AgentDeps(llm=llm, toolbox=LocalToolbox(ctx), retriever=ret, brand_voice=load_brand_voice(ROOT / "data"))


def R(label, conf=0.95):
    return {"label": label, "confidence": conf, "reason": "test"}


def save(body, sources=()):
    return FunctionCall(name="save_draft", args={"subject": "Re: x", "body": body, "sources": list(sources)})


# ---------------------------------------------------------------- router


def test_router_returns_structured_decision_from_untrusted_block():
    llm = FakeLLM({"router": [R("question", 0.92)]})
    d = asyncio.run(route(ADAPTER.parse(EMAILS / "f02_faq_delivery.eml"), llm))
    assert d == RouteDecision(label="question", confidence=0.92, reason="test")
    assert "<untrusted_email>" in llm.calls[0]["turns"][0].text


def test_router_rejects_invalid_label():
    llm = FakeLLM({"router": [{"label": "urgent", "confidence": 0.9, "reason": "x"}]})
    with pytest.raises(LLMError):
        asyncio.run(route(ADAPTER.parse(EMAILS / "f02_faq_delivery.eml"), llm))


@pytest.mark.parametrize("conf,expected", [(0.69, True), (0.7, False), (0.95, False)])
def test_low_confidence_threshold(conf, expected):
    assert needs_owner(RouteDecision(label="other", confidence=conf, reason=""), 0.7) is expected


# ---------------------------------------------------------------- FAQ / other agents


def test_faq_agent_answers_with_retrieved_citations(index):
    ret = retriever(index)
    hits = asyncio.run(ret.retrieve("wedding gallery delivered weeks"))
    cid = hits[0].chunk_id
    llm = FakeLLM({"faq_agent": [
        FunctionCall(name="search_policies", args={"query": "wedding gallery delivered weeks"}),
        save(f"Hi Chris, wedding galleries arrive within 6 weeks. [{cid}]", [cid]),
    ]})
    res = asyncio.run(run_faq_agent(ADAPTER.parse(EMAILS / "f02_faq_delivery.eml"), deps(llm, ret)))
    assert res.status == "drafted" and res.draft.sources == [cid]
    assert cid in res.retrieved_chunk_ids


def test_faq_agent_says_it_does_not_know_when_nothing_retrieved(index):
    ctx = ToolContext.from_data_dir(ROOT / "data")
    llm = FakeLLM({"faq_agent": [
        FunctionCall(name="search_policies", args={"query": "drone rental"}),
        save("Sure, we rent drones for $50 a day!"),  # an improvised answer must never survive
    ]})
    res = asyncio.run(run_faq_agent(ADAPTER.parse(EMAILS / "f02_faq_delivery.eml"), deps(llm, retriever(index, 0.99), ctx)))
    assert res.status == "needs_owner" and res.flag_owner
    assert "$50" not in res.draft.body and "get back to you" in res.draft.body
    assert "unanswered" in res.reason
    assert ctx.saved[-1].body == res.draft.body  # the holding reply is what gets stored


def test_faq_agent_rejects_citation_that_was_never_retrieved(index):
    llm = FakeLLM({"faq_agent": [
        FunctionCall(name="search_policies", args={"query": "wedding gallery delivered weeks"}),
        save("Galleries arrive in 1 day. [delivery-timelines#made-up#0]", ["delivery-timelines#made-up#0"]),
    ]})
    res = asyncio.run(run_faq_agent(ADAPTER.parse(EMAILS / "f02_faq_delivery.eml"), deps(llm, retriever(index))))
    assert res.status == "needs_owner" and "never retrieved" in res.reason


def test_other_agent_always_flags_owner(index):
    llm = FakeLLM({"other_agent": [
        FunctionCall(name="search_policies", args={"query": "reschedule portrait session"}),
        save("Hi Laura, thanks for letting me know. I'll check the calendar and confirm personally."),
    ]})
    res = asyncio.run(run_other_agent(ADAPTER.parse(EMAILS / "r01_reschedule_portrait.eml"), deps(llm, retriever(index)), "reschedule"))
    assert res.status == "drafted" and res.flag_owner and "owner must confirm" in res.reason


def test_cited_ids_parser():
    assert cited_ids("a [x-y#z#0] b [x-y#z#0] [q#r#12] [not a cite]") == ["x-y#z#0", "q#r#12"]


# ---------------------------------------------------------------- state machine


def _store_with(status, tmp_path):
    store = Store(tmp_path / "s.db")
    inq = ADAPTER.parse(EMAILS / "q01_mini_weekday.eml")
    store.add_inquiry(inq, status=status.value)
    return store, inq.id


@pytest.mark.parametrize("frm", list(S))
def test_every_transition_obeys_the_table(frm, tmp_path):
    for to in S:
        store, iid = _store_with(frm, tmp_path / f"{frm.value}-{to.value}")
        if to in TRANSITIONS[frm]:
            transition(store, iid, to, "ok")
            assert store.get_status(iid) == to.value
        else:
            with pytest.raises(InvalidTransition):
                transition(store, iid, to)
            assert store.get_status(iid) == frm.value


def test_cannot_skip_approval(tmp_path):
    for frm in (S.RECEIVED, S.ROUTED, S.DRAFTED, S.PENDING_APPROVAL, S.OWNER_QUEUE, S.BLOCKED, S.REJECTED):
        assert S.SENT not in TRANSITIONS[frm]


def test_transitions_are_logged(tmp_path):
    store, iid = _store_with(S.RECEIVED, tmp_path)
    transition(store, iid, S.ROUTED, "quote_request (0.95)")
    log = store.transitions(iid)
    assert [(t["from_status"], t["to_status"]) for t in log] == [(None, "RECEIVED"), ("RECEIVED", "ROUTED")]


# ---------------------------------------------------------------- pipeline


Q01_FIELDS = {"service_type": "mini_portrait", "event_date": "2026-12-09", "location": "Maple Hollow"}


def pipeline(scripts, tmp_path, ret=None, **kw):
    store = Store(tmp_path / "p.db")
    ctx = ToolContext.from_data_dir(ROOT / "data", save_draft_fn=store.add_draft, today=date(2026, 11, 10))
    return Pipeline(store=store, deps=deps(FakeLLM(scripts), ret, ctx), settings=get_settings(), **kw), store


def test_quote_flows_to_pending_approval(tmp_path, sink):
    p, store = pipeline({"router": [R("quote_request")], "extractor": [Q01_FIELDS], "quote_agent": happy_script()}, tmp_path)
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "q01_mini_weekday.eml")))
    assert out.status == S.PENDING_APPROVAL and out.agent == "quote" and not out.needs_info
    assert [t["to_status"] for t in store.transitions(out.inquiry_id)] == ["RECEIVED", "ROUTED", "DRAFTED", "PENDING_APPROVAL"]
    row = store.get_row(out.inquiry_id)
    assert row["route"] and row["extracted"] and store.latest_draft(out.inquiry_id)
    names = {s.name for s in sink.spans if s.trace_id == out.inquiry_id}
    assert {"pipeline", "input_guardrails", "router", "extract_fields", "quote_agent", "output_guardrails"} <= names


def test_spam_is_blocked_without_a_draft(tmp_path):
    p, store = pipeline({"router": [R("spam")]}, tmp_path)
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "s01_spam.eml")))
    assert out.status == S.BLOCKED and store.latest_draft(out.inquiry_id) is None


def test_low_confidence_goes_to_owner_queue(tmp_path):
    p, store = pipeline({"router": [R("other", 0.4)]}, tmp_path)
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "v01_vague_price.eml")))
    assert out.status == S.OWNER_QUEUE and "confidence" in store.get_row(out.inquiry_id)["owner_note"]


def test_input_guard_block_stops_before_router(tmp_path):
    async def block(inq):
        return InputCheck(allowed=False, reason="prompt injection", verdicts=[GuardrailVerdict(name="injection", passed=False)])
    p, store = pipeline({}, tmp_path, input_guard=block)  # FakeLLM has no scripts: any LLM call would fail
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "i01_injection_blatant.eml")))
    assert out.status == S.BLOCKED and store.get_row(out.inquiry_id)["route"] is None


def test_output_guard_failure_goes_to_owner_queue(tmp_path):
    async def fail(inq, draft, result):
        return OutputCheck(passed=False, verdicts=[GuardrailVerdict(name="amounts_match_tools", passed=False)])
    p, store = pipeline({"router": [R("quote_request")], "extractor": [Q01_FIELDS], "quote_agent": happy_script()},
                        tmp_path, output_guard=fail)
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "q01_mini_weekday.eml")))
    assert out.status == S.OWNER_QUEUE and "amounts_match_tools" in out.reason


def test_unexpected_error_lands_in_owner_queue(tmp_path):
    p, store = pipeline({"router": [R("quote_request")]}, tmp_path)  # no extractor script -> KeyError
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "q01_mini_weekday.eml")))
    assert out.status == S.OWNER_QUEUE and "pipeline error" in out.reason


def test_reschedule_is_drafted_and_flagged(tmp_path, index):
    p, store = pipeline({"router": [R("reschedule")], "other_agent": [
        FunctionCall(name="search_policies", args={"query": "reschedule portrait"}),
        save("Hi Laura, I'll confirm personally."),
    ]}, tmp_path, ret=retriever(index))
    out = asyncio.run(p.process(ADAPTER.parse(EMAILS / "r01_reschedule_portrait.eml")))
    assert out.status == S.PENDING_APPROVAL and out.flag_owner
    assert "owner must confirm" in store.get_row(out.inquiry_id)["owner_note"]
