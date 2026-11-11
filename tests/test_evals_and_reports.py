import asyncio
from datetime import date

from rich.console import Console

from app.adapters.eml import EmlAdapter
from app.agents.base import AgentResult
from app.evaluation.golden import CaseResult, EvalReport, check_targets, compute_metrics, predicted_route, score_case
from app.evaluation.judge import JudgeVerdict, judge_draft
from app.evaluation.reporting import compare, to_markdown
from app.llm.fake import FakeLLM
from app.models import Decision, Draft, ExtractedFields, InquiryStatus as S, RouteDecision, ToolCallRecord
from app.observability import tracing
from app.observability.report import ops_report, render_trace
from app.orchestrator import PipelineOutcome, record_decision
from app.store import Store
from conftest import EMAILS

Q04 = {"file": "q04_event_sunday_second.eml", "expected_route": "quote_request",
       "expected_fields": {"service_type": "event", "event_date": date(2027, 2, 14), "location": "Willowbrook",
                           "hours": 3, "add_ons": ["second_photographer"]},
       "expected_clarification": False, "expected_available": False,
       "expected_total": 1142.50, "expected_deposit": 342.75}


def _outcome(total=1142.5, body_total="$1,142.50", hours=3.0, status=S.PENDING_APPROVAL, reason=""):
    calls = [
        ToolCallRecord(name="check_availability", args={"date": "2027-02-14"}, result={"ok": True, "status": "booked"}),
        ToolCallRecord(name="calculate_quote", args={}, result={"ok": True, "total": total, "deposit": 342.75}),
    ]
    return PipelineOutcome(
        inquiry_id="inq_1", status=status, reason=reason,
        route=RouteDecision(label="quote_request", confidence=0.98, reason=""),
        fields=ExtractedFields(service_type="event", event_date=date(2027, 2, 14), location="willowbrook",
                               hours=hours, add_ons=["second_photographer"]),
        draft=Draft(inquiry_id="inq_1", subject="Re", body=f"Total: {body_total}"),
        agent_result=AgentResult(agent="quote", status="drafted", tool_calls=calls),
    )


def test_score_case_happy(tmp_path):
    r = score_case(Q04, _outcome(), Store(tmp_path / "e.db"))
    assert r.route_ok and r.quote_ok and r.total_in_draft
    assert all(r.field_results.values())  # location compared case-insensitively
    assert r.availability_status == "booked"


def test_score_case_catches_wrong_total_field_and_hidden_total(tmp_path):
    r = score_case(Q04, _outcome(total=1000.0, hours=4.0), Store(tmp_path / "e.db"))
    assert r.quote_ok is False and r.field_results["hours"] is False
    r2 = score_case(Q04, _outcome(body_total="(see attachment)"), Store(tmp_path / "e2.db"))
    assert r2.quote_ok is False and r2.total_in_draft is False


def test_predicted_route_maps_guardrail_blocks():
    assert predicted_route(PipelineOutcome(inquiry_id="x", status=S.BLOCKED, reason="prompt injection suspected")) == "blocked"
    assert predicted_route(PipelineOutcome(inquiry_id="x", status=S.BLOCKED, reason="spam")) == "spam"


def _case(file, route_ok=True, **kw):
    base = dict(file=file, inquiry_id="i", status="PENDING_APPROVAL", expected_route="question",
                predicted_route="question" if route_ok else "other", route_ok=route_ok)
    return CaseResult(**(base | kw))


def test_metrics_and_targets():
    cases = [
        _case("a", expected_total=10, quote_ok=True, expected_clarification=False),
        _case("b", route_ok=False, expected_clarification=True, needs_info=True),
        _case("c", expected_clarification=False, needs_info=True),
        _case("d", expected_injection=True, predicted_route="blocked", expected_route="blocked", status="BLOCKED"),
        _case("e", judge=JudgeVerdict(score=5, addresses_request=True, reasoning="")),
        _case("f", judge=JudgeVerdict(score=2, addresses_request=True, reasoning="", unsupported_claims=["x"])),
    ]
    m = compute_metrics(cases, unapproved_sends=0, retrieval={"hit_rate": 1.0, "mrr": 0.9})
    assert m["routing_accuracy"] == round(5 / 6, 3)
    assert m["quote_correct"] == "1/1" and m["injection_blocked_rate"] == 1.0
    assert m["clarification_precision"] == 0.5 and m["clarification_recall"] == 1.0
    assert m["groundedness_rate"] == 0.5 and m["groundedness_mean_score"] == 3.5
    t = check_targets(m)
    assert t["routing_accuracy"] is False and t["unapproved_sends"] is True


def _report(run_id, cases, metrics, prompts=None):
    return EvalReport(run_id=run_id, created_at="2026-11-11T00:00:00", chat_model="m", embedding_model="e",
                      prompt_versions=prompts or {"router": "1"}, n_cases=len(cases), metrics=metrics,
                      targets_met=check_targets(metrics), cases=cases, db_path="x.db")


def test_markdown_and_compare_flag_regressions():
    a_cases = [_case("q.eml", expected_total=10.0, actual_total=10.0, quote_ok=True)]
    b_cases = [_case("q.eml", route_ok=False, expected_total=10.0, actual_total=9.0, quote_ok=False)]
    a = _report("A", a_cases, compute_metrics(a_cases, 0, None))
    b = _report("B", b_cases, compute_metrics(b_cases, 0, None), prompts={"router": "2"})
    md = to_markdown(b)
    assert "Routing accuracy" in md and "❌" in md and "q.eml" in md
    diff = compare(a, b)
    assert "router v1→v2" in diff and "regression" in diff
    assert "route question → other" in diff and "quote_ok True → False" in diff


def test_judge_uses_rubric_and_evidence():
    inq = EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")
    draft = Draft(inquiry_id=inq.id, subject="Re", body="Total $175.00", tool_results=[
        ToolCallRecord(name="calculate_quote", args={"package": "mini_portrait"}, result={"ok": True, "total": 175.0}),
        ToolCallRecord(name="search_policies", args={"query": "d"}, result={"ok": True, "chunks": [
            {"chunk_id": "a#b#0", "text": "Deposit is 30%.", "heading": "h", "score": 0.7}]}),
    ])
    llm = FakeLLM({"judge": [{"score": 5, "unsupported_claims": [], "addresses_request": True, "reasoning": "ok"}]})
    v = asyncio.run(judge_draft(inq, draft, llm))
    assert v.grounded
    msg = llm.calls[0]["turns"][0].text
    assert "TOOL calculate_quote" in msg and "POLICY [a#b#0]" in msg and "<untrusted_email>" in msg
    assert "Rubric" in llm.calls[0]["system"]


def test_trace_render_and_ops_report(tmp_path):
    store = Store(tmp_path / "o.db")
    tracing.set_sink(store)
    adapter = EmlAdapter()
    inq = adapter.parse(EMAILS / "q01_mini_weekday.eml")
    store.add_inquiry(inq, status="PENDING_APPROVAL")
    store.set_field(inq.id, "route", {"label": "quote_request", "confidence": 0.9, "reason": ""})
    store.add_draft(Draft(inquiry_id=inq.id, subject="Re", body="Hi"))
    with tracing.trace(inq.id, name="pipeline"):
        with tracing.span("tool.calculate_quote") as s:
            s.tool({"package": "mini_portrait"}, {"total": 175.0})
            tracing.record_llm_usage(100, 10, 0.001)
    record_decision(store, Decision(inquiry_id=inq.id, action="approve"))
    faq = adapter.parse(EMAILS / "f02_faq_delivery.eml")
    store.add_inquiry(faq, status="OWNER_QUEUE")
    store.set_field(faq.id, "owner_note", "no policy found above the retrieval threshold (unanswered topic)")

    console = Console(record=True, width=120)
    render_trace(store.spans_for_trace(inq.id), console)
    out = console.export_text()
    assert "pipeline" in out and "tool.calculate_quote" in out and '"total": 175.0' in out

    md = ops_report(store)
    assert "**Volume:** 2 inquiries" in md
    assert "| quote_request | 1 | 50% |" in md
    assert "Approved as drafted: 1" in md
    assert "How long for wedding photos?" in md  # unanswered topic surfaced


def test_compare_ignores_small_latency_noise():
    cases = [_case("q.eml")]
    ma, mb = compute_metrics(cases, 0, None), compute_metrics(cases, 0, None)
    ma["avg_latency_s"], mb["avg_latency_s"] = 28.8, 29.3
    ma["avg_cost_usd"], mb["avg_cost_usd"] = 0.0088, 0.0120
    diff = compare(_report("A", cases, ma), _report("B", cases, mb))
    latency = next(line for line in diff.splitlines() if "latency" in line)
    cost = next(line for line in diff.splitlines() if "Average cost" in line)
    assert "noise" in latency and "regression" in cost
