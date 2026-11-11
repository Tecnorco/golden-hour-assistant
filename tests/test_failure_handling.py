"""Failure paths: rate limits, a crashing repair step, a failing eval judge.
(The MCP server crash/restart tests live in test_mcp.py.)"""

import asyncio
from datetime import date
from pathlib import Path

from google.genai import errors as genai_errors
from pydantic import SecretStr

from app.adapters.eml import EmlAdapter
from app.agents.base import AgentDeps, load_brand_voice
from app.config import get_settings
from app.evaluation.golden import run_golden_eval
from app.evaluation.reporting import to_markdown
from app.llm.client import FunctionCall, GeminiLLM, LLMError
from app.llm.fake import FakeLLM
from app.models import GuardrailVerdict
from app.orchestrator import OutputCheck, Pipeline
from app.rag.retriever import Retriever, build_index
from app.store import Store
from app.tools import LocalToolbox, ToolContext
from conftest import EMAILS
from fakes import FakeEmbedder
from test_quote_agent import happy_script

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 1. rate limits


def test_rate_limit_429_is_retried_with_backoff(monkeypatch):
    s = get_settings().model_copy(update={"api_key": SecretStr("dummy"), "llm_retry_attempts": 4})
    llm, calls, sleeps = GeminiLLM(s), {"n": 0}, []

    async def rate_limited():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise genai_errors.ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})
        return "ok"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    assert asyncio.run(llm._with_retry(rate_limited, "t")) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff


def test_persistent_rate_limit_fails_cleanly_after_all_attempts(monkeypatch):
    s = get_settings().model_copy(update={"api_key": SecretStr("dummy"), "llm_retry_attempts": 3})
    llm = GeminiLLM(s)

    async def always_429():
        raise genai_errors.ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(llm._with_retry(always_429, "router"))
        raise AssertionError("expected LLMError")
    except LLMError as exc:
        assert "3 attempt" in str(exc) and "429" in str(exc)


# ---------------------------------------------------------------- 2. repair step crashes


def _pipeline(store, **kw):
    ctx = ToolContext.from_data_dir(ROOT / "data", save_draft_fn=store.add_draft, today=date(2026, 11, 10))
    fake = FakeLLM({"router": [{"label": "quote_request", "confidence": 0.95, "reason": "x"}],
                    "extractor": [{"service_type": "mini_portrait", "event_date": "2026-12-09", "location": "Maple Hollow"}],
                    "quote_agent": happy_script()})
    deps = AgentDeps(llm=fake, toolbox=LocalToolbox(ctx), retriever=None, brand_voice=load_brand_voice(ROOT / "data"))
    return Pipeline(store=store, deps=deps, settings=get_settings(), **kw)


async def _failing_guard(inq, draft, result):
    return OutputCheck(passed=False, verdicts=[GuardrailVerdict(name="amounts_match_tools", passed=False)])


def test_crashing_repair_step_sends_item_to_owner_queue_not_customer(tmp_path):
    async def broken_repair(*args):
        raise LLMError("repair model unavailable")

    store = Store(tmp_path / "r.db")
    out = asyncio.run(_pipeline(store, output_guard=_failing_guard, repair=broken_repair).process(
        EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")))
    assert out.status.value == "OWNER_QUEUE"
    assert "repair model unavailable" in store.get_row(out.inquiry_id)["owner_note"]
    assert store.get_status(out.inquiry_id) != "SENT"


def test_repair_returning_nothing_also_goes_to_owner_queue(tmp_path):
    async def gives_up(*args):
        return None

    store = Store(tmp_path / "r2.db")
    out = asyncio.run(_pipeline(store, output_guard=_failing_guard, repair=gives_up).process(
        EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")))
    assert out.status.value == "OWNER_QUEUE" and "amounts_match_tools" in out.reason


# ---------------------------------------------------------------- 4. eval judge fails


def test_failing_judge_marks_case_not_judged_and_eval_completes(capsys):
    body = ("Hi Jenna,\n\nHere is what that looks like:\n\n- Mini Portrait Session: $175.00\nTotal: $175.00\n"
            "Deposit (30%): $52.50\n\nJust reply and I'll send the deposit invoice.\n\nWarmly,\nMaya\nGolden Hour Photography")
    fake = FakeLLM({
        "injection_classifier": [{"is_injection": False, "confidence": 0.02, "evidence": ""}],
        "router": [{"label": "quote_request", "confidence": 0.97, "reason": "asks for a price"}],
        "extractor": [{"service_type": "mini_portrait", "event_date": "2026-12-09", "location": "Maple Hollow"}],
        "quote_agent": [
            [FunctionCall(name="lookup_distance", args={"town": "Maple Hollow"}),
             FunctionCall(name="check_availability", args={"date": "2026-12-09"})],
            FunctionCall(name="calculate_quote", args={"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 8.0}),
            FunctionCall(name="save_draft", args={"subject": "Re: Mini session", "body": body}),
        ],
        # no "judge" script: every judge call raises, like a network failure
    })
    s = get_settings()
    retriever = Retriever(asyncio.run(build_index(s, FakeEmbedder())), FakeEmbedder(), 3, 0.1)
    report = asyncio.run(run_golden_eval(s, fake, retriever, ROOT / "evals" / "golden.yaml",
                                         run_id="judge-fails", concurrency=1, judge=True, only="q01"))
    (case,) = report.cases
    assert case.judge is None and case.judge_error and "KeyError" in case.judge_error
    assert case.quote_ok is True and case.route_ok  # everything else was still scored
    assert report.metrics["judge_errors"] == 1 and report.metrics["groundedness_rate"] is None
    md = to_markdown(report)
    assert "Cases not judged (judge call failed) | 1" in md and "not judged:" in md
