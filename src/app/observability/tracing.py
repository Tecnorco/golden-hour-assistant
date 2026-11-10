"""Minimal tracing: nested spans via contextvars, persisted to a pluggable sink.

Usage:
    with trace(inquiry.id):                # root; trace_id == inquiry id
        with span("router", input=...) as s:
            ...
            s.output = decision

The LLM wrapper calls `record_llm_usage` so token counts and cost land on
whatever span is currently open.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from app.guardrails.redact import redact, summarize
from app.models import GuardrailVerdict, TraceSpan, new_id, utcnow


class SpanSink(Protocol):
    def add_span(self, span: TraceSpan) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.spans: list[TraceSpan] = []

    def add_span(self, span: TraceSpan) -> None:
        self.spans.append(span)


_sink: SpanSink = MemorySink()
_current: ContextVar[TraceSpan | None] = ContextVar("current_span", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_sink(sink: SpanSink) -> None:
    global _sink
    _sink = sink


def get_sink() -> SpanSink:
    return _sink


def current_span() -> TraceSpan | None:
    return _current.get()


def current_trace_id() -> str | None:
    return _trace_id.get()


class SpanHandle:
    """What `with span(...) as s` yields; setters redact before storing."""

    def __init__(self, span: TraceSpan) -> None:
        self._span = span

    @property
    def span_id(self) -> str:
        return self._span.span_id

    @property
    def trace_id(self) -> str:
        return self._span.trace_id

    @property
    def output(self) -> str:
        return self._span.output_summary

    @output.setter
    def output(self, value: object) -> None:
        self._span.output_summary = summarize(value)

    def tool(self, args: dict[str, Any], result: Any) -> None:
        self._span.tool_args = _redact_obj(args)
        self._span.tool_result = _redact_obj(result)

    def verdict(self, v: GuardrailVerdict) -> None:
        self._span.verdicts.append(v.model_copy(update={"details": redact(v.details)}))

    def set(self, **attrs: Any) -> None:
        self._span.attributes.update(_redact_obj(attrs))


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return _redact_obj(obj.model_dump(mode="json"))
    return obj


@contextmanager
def span(name: str, input: object = None) -> Iterator[SpanHandle]:
    trace_id = _trace_id.get() or new_id("trace")
    parent = _current.get()
    s = TraceSpan(
        span_id=new_id("span"),
        trace_id=trace_id,
        parent_id=parent.span_id if parent else None,
        name=name,
        start=utcnow(),
        input_summary=summarize(input) if input is not None else "",
    )
    t0 = time.perf_counter()
    tok_trace = _trace_id.set(trace_id)
    tok_span = _current.set(s)
    try:
        yield SpanHandle(s)
    except BaseException as exc:
        s.error = redact(f"{type(exc).__name__}: {exc}")[:500]
        raise
    finally:
        _current.reset(tok_span)
        _trace_id.reset(tok_trace)
        s.end = utcnow()
        s.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        # Children roll their usage up so parents show totals.
        if parent is not None:
            parent.tokens_in += s.tokens_in
            parent.tokens_out += s.tokens_out
            parent.cost_usd += s.cost_usd
        _sink.add_span(s)


@contextmanager
def trace(trace_id: str, name: str = "pipeline", input: object = None) -> Iterator[SpanHandle]:
    """Start a root span with a caller-chosen trace id (we use the inquiry id)."""
    tok = _trace_id.set(trace_id)
    try:
        with span(name, input=input) as s:
            yield s
    finally:
        _trace_id.reset(tok)


def record_llm_usage(tokens_in: int, tokens_out: int, cost_usd: float, **attrs: Any) -> None:
    s = _current.get()
    if s is None:
        return
    s.tokens_in += tokens_in
    s.tokens_out += tokens_out
    s.cost_usd += cost_usd
    calls = s.attributes.setdefault("llm_calls", [])
    calls.append(_redact_obj(attrs))
