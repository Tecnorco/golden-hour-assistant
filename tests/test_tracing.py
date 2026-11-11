import pytest

from app.models import GuardrailVerdict
from app.observability.tracing import record_llm_usage, span, trace
from app.store import Store


def test_nested_spans_share_trace_and_link_parents(sink):
    with trace("inq_1", input="hello") as root:
        with span("child") as c:
            with span("grandchild"):
                record_llm_usage(100, 20, 0.01, model="m")
            c.output = "done"
    by_name = {s.name: s for s in sink.spans}
    assert {s.trace_id for s in sink.spans} == {"inq_1"}
    assert by_name["grandchild"].parent_id == by_name["child"].span_id
    assert by_name["child"].parent_id == root.span_id
    assert by_name["pipeline"].parent_id is None
    # usage rolls up to every ancestor
    assert by_name["pipeline"].tokens_in == 100 and by_name["child"].cost_usd == pytest.approx(0.01)
    assert all(s.latency_ms is not None for s in sink.spans)


def test_error_is_recorded_and_reraised(sink):
    with pytest.raises(ValueError):
        with trace("inq_2"):
            with span("boom"):
                raise ValueError("bad thing for jenna@example.com")
    boom = next(s for s in sink.spans if s.name == "boom")
    assert boom.error and "ValueError" in boom.error
    assert "jenna@example.com" not in boom.error


def test_span_fields_are_redacted(sink):
    with trace("inq_3", input="from tom@example.net (555) 214-8890") as s:
        s.tool({"email": "tom@example.net"}, {"ok": True})
        s.verdict(GuardrailVerdict(name="x", passed=False, details="phone 555-214-8890"))
    root = sink.spans[0]
    assert "tom@example.net" not in root.input_summary and "214" not in root.input_summary
    assert root.tool_args == {"email": "[EMAIL]"}
    assert "214" not in root.verdicts[0].details


def test_spans_persist_to_sqlite(tmp_path):
    from app.observability import tracing

    store = Store(tmp_path / "t.db")
    tracing.set_sink(store)
    with trace("inq_4"):
        with span("step"):
            pass
    spans = store.spans_for_trace("inq_4")
    assert [s.name for s in spans] == ["pipeline", "step"]
