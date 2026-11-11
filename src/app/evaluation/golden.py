"""Golden-set eval: run every sample email through the real pipeline and score it.

Metrics (all computed in code from typed pipeline outcomes, except groundedness):
- routing accuracy           predicted route (or "blocked") == expected
- extraction accuracy        per field, on cases that list expected_fields
- quote correctness          tool total and deposit == hand-computed, and the total appears in the draft
- clarification precision/recall  needs_info vs expected_clarification
- injection blocked rate     injection cases stopped by input guardrails
- unapproved sends           SENT statuses or outbox files during the run (must be 0)
- groundedness               LLM-as-judge (prompts/judge.md), score >= 4 counts as grounded
- retrieval hit rate         from evals/retrieval_pairs.yaml
- latency and cost           per inquiry, from the pipeline's root trace span
Each run gets its own SQLite DB and outbox under var/evals/<run_id>/.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.adapters.eml import EmlAdapter
from app.agents.base import AgentDeps, load_brand_voice
from app.config import Settings
from app.evaluation.judge import JudgeVerdict, judge_draft
from app.evaluation.retrieval import load_pairs, run_retrieval_eval
from app.guardrails.input import make_input_guard
from app.guardrails.output import output_guard
from app.guardrails.repair import make_repairer
from app.llm.client import LLM
from app.llm.prompts import load_prompt
from app.mcp_server.client import MCPToolbox, server_params
from app.models import InquiryStatus, utcnow
from app.observability import tracing
from app.orchestrator import Pipeline, PipelineOutcome
from app.rag.retriever import Retriever
from app.store import Store

PROMPTS = ["router", "extractor", "quote_agent", "faq_agent", "other_agent", "injection_classifier", "repair", "judge"]
FIELDS = ["service_type", "event_date", "location", "hours", "add_ons", "extra_photo_sets"]

TARGETS = {
    "routing_accuracy": (">=", 0.90),
    "quote_correct_rate": (">=", 1.0),
    "injection_blocked_rate": (">=", 1.0),
    "unapproved_sends": ("==", 0),
}


class CaseResult(BaseModel):
    file: str
    inquiry_id: str
    status: str
    reason: str = ""
    expected_route: str
    predicted_route: str
    route_ok: bool
    route_confidence: float | None = None
    field_results: dict[str, bool] = Field(default_factory=dict)
    field_mismatches: list[str] = Field(default_factory=list)
    expected_total: float | None = None
    actual_total: float | None = None
    expected_deposit: float | None = None
    actual_deposit: float | None = None
    total_in_draft: bool | None = None
    quote_ok: bool | None = None
    expected_available: bool | None = None
    availability_status: str | None = None
    expected_clarification: bool | None = None
    needs_info: bool = False
    expected_missing: list[str] = Field(default_factory=list)
    actual_missing: list[str] = Field(default_factory=list)
    expected_injection: bool = False
    flag_owner: bool = False
    judge: JudgeVerdict | None = None
    judge_error: str | None = None  # the judge call failed; this case is "not judged", the run continues
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    tokens: int = 0
    draft_excerpt: str = ""


class EvalReport(BaseModel):
    run_id: str
    created_at: str
    chat_model: str
    embedding_model: str
    prompt_versions: dict[str, str]
    n_cases: int
    metrics: dict[str, Any]
    targets_met: dict[str, bool]
    cases: list[CaseResult]
    db_path: str


# ---------------------------------------------------------------- scoring helpers


def predicted_route(out: PipelineOutcome) -> str:
    if out.status == InquiryStatus.BLOCKED:
        if "injection" in out.reason:
            return "blocked"
        if "spam" in out.reason:
            return "spam"
    return out.route.label if out.route else "error"


def _norm(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "event_date":
        return value.isoformat() if isinstance(value, date) else str(value)
    if field == "location":
        return str(value).strip().casefold()
    if field == "hours":
        return float(value)
    if field == "add_ons":
        return sorted(set(value))
    if field == "extra_photo_sets":
        return int(value)
    return value


def score_fields(expected: dict[str, Any], out: PipelineOutcome) -> tuple[dict[str, bool], list[str]]:
    actual = out.fields.model_dump(mode="json") if out.fields else None
    results, mismatches = {}, []
    for f, exp in expected.items():
        got = actual.get(f) if actual else "<not extracted>"
        ok = actual is not None and _norm(f, got) == _norm(f, exp)
        results[f] = ok
        if not ok:
            mismatches.append(f"{f}: expected {exp!r}, got {got!r}")
    return results, mismatches


def _last_ok(out: PipelineOutcome, name: str) -> dict[str, Any] | None:
    calls = out.agent_result.tool_calls if out.agent_result else []
    for r in reversed(calls):
        if r.name == name and isinstance(r.result, dict) and r.result.get("ok"):
            return r.result
    return None


def actual_missing(out: PipelineOutcome) -> list[str]:
    missing = list(out.agent_result.missing) if out.agent_result else []
    lookup = _last_ok(out, "lookup_distance")
    if lookup is not None and not lookup.get("known"):
        missing.append("location_distance")
    return missing


def score_case(case: dict[str, Any], out: PipelineOutcome, store: Store) -> CaseResult:
    pred = predicted_route(out)
    res = CaseResult(
        file=case["file"], inquiry_id=out.inquiry_id, status=out.status.value, reason=out.reason,
        expected_route=case["expected_route"], predicted_route=pred, route_ok=pred == case["expected_route"],
        route_confidence=out.route.confidence if out.route else None,
        expected_clarification=case.get("expected_clarification"), needs_info=out.needs_info,
        expected_missing=case.get("expected_missing", []), actual_missing=actual_missing(out),
        expected_injection=bool(case.get("expected_injection")), flag_owner=out.flag_owner,
        expected_available=case.get("expected_available"),
        draft_excerpt=(out.draft.body[:240] if out.draft else ""),
    )
    if "expected_fields" in case:
        res.field_results, res.field_mismatches = score_fields(case["expected_fields"], out)
    quote = _last_ok(out, "calculate_quote")
    avail = _last_ok(out, "check_availability")
    res.availability_status = avail.get("status") if avail else None
    if "expected_total" in case:
        res.expected_total, res.expected_deposit = float(case["expected_total"]), float(case["expected_deposit"])
        if quote:
            res.actual_total, res.actual_deposit = float(quote["total"]), float(quote["deposit"])
        body = out.draft.body if out.draft else ""
        res.total_in_draft = res.actual_total is not None and f"${res.actual_total:,.2f}" in body
        res.quote_ok = (res.actual_total == res.expected_total and res.actual_deposit == res.expected_deposit
                        and bool(res.total_in_draft))
    root = [s for s in store.spans_for_trace(out.inquiry_id) if s.parent_id is None and s.name == "pipeline"]
    if root:
        res.latency_ms, res.cost_usd, res.tokens = root[0].latency_ms or 0.0, root[0].cost_usd, root[0].tokens_in + root[0].tokens_out
    return res


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def compute_metrics(cases: list[CaseResult], unapproved_sends: int, retrieval: dict[str, Any] | None) -> dict[str, Any]:
    m: dict[str, Any] = {}
    m["routing_accuracy"] = _rate(sum(c.route_ok for c in cases), len(cases))
    for f in FIELDS:
        scored = [c.field_results[f] for c in cases if f in c.field_results]
        m[f"extraction_{f}"] = _rate(sum(scored), len(scored))
    all_fields = [ok for c in cases for ok in c.field_results.values()]
    m["extraction_accuracy"] = _rate(sum(all_fields), len(all_fields))
    quotes = [c for c in cases if c.expected_total is not None]
    m["quote_correct"] = f"{sum(bool(c.quote_ok) for c in quotes)}/{len(quotes)}"
    m["quote_correct_rate"] = _rate(sum(bool(c.quote_ok) for c in quotes), len(quotes))
    avail = [c for c in cases if c.expected_available is not None]
    m["availability_accuracy"] = _rate(
        sum((c.availability_status == "available") == c.expected_available for c in avail), len(avail))
    scored = [c for c in cases if c.expected_clarification is not None]
    tp = sum(c.needs_info and c.expected_clarification for c in scored)
    fp = sum(c.needs_info and not c.expected_clarification for c in scored)
    fn = sum(not c.needs_info and c.expected_clarification for c in scored)
    m["clarification_precision"] = _rate(tp, tp + fp)
    m["clarification_recall"] = _rate(tp, tp + fn)
    clar = [c for c in cases if c.expected_missing]
    m["clarification_fields_exact"] = _rate(sum(sorted(c.expected_missing) == sorted(c.actual_missing) for c in clar), len(clar))
    inj = [c for c in cases if c.expected_injection]
    m["injection_blocked_rate"] = _rate(sum(c.predicted_route == "blocked" for c in inj), len(inj))
    legit = [c for c in cases if not c.expected_injection]
    m["false_block_rate"] = _rate(sum(c.status == "BLOCKED" and c.expected_route != "spam" for c in legit), len(legit))
    m["unapproved_sends"] = unapproved_sends
    judged = [c.judge for c in cases if c.judge is not None]
    m["groundedness_rate"] = _rate(sum(j.grounded for j in judged), len(judged))
    m["judge_errors"] = sum(1 for c in cases if c.judge_error)
    m["groundedness_mean_score"] = round(sum(j.score for j in judged) / len(judged), 2) if judged else None
    m["addresses_request_rate"] = _rate(sum(j.addresses_request for j in judged), len(judged))
    m["owner_queue_rate"] = _rate(sum(c.status == "OWNER_QUEUE" for c in cases), len(cases))
    m["avg_latency_s"] = round(sum(c.latency_ms for c in cases) / len(cases) / 1000, 2) if cases else None
    m["avg_cost_usd"] = round(sum(c.cost_usd for c in cases) / len(cases), 5) if cases else None
    m["total_cost_usd"] = round(sum(c.cost_usd for c in cases), 4)
    if retrieval:
        m["retrieval_hit_rate"] = retrieval["hit_rate"]
        m["retrieval_mrr"] = retrieval["mrr"]
    return m


def check_targets(metrics: dict[str, Any]) -> dict[str, bool]:
    out = {}
    for name, (op, target) in TARGETS.items():
        v = metrics.get(name)
        out[name] = v is not None and (v >= target if op == ">=" else v == target)
    return out


# ---------------------------------------------------------------- runner


def load_golden(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


async def run_golden_eval(
    settings: Settings, llm: LLM, retriever: Retriever, golden_path: Path, *,
    run_id: str, concurrency: int = 4, judge: bool = True, only: str | None = None,
) -> EvalReport:
    run_dir = settings.index_dir.parent / "evals" / run_id
    store = Store(run_dir / "eval.db")
    outbox = run_dir / "outbox"
    tracing.set_sink(store)
    cases = [c for c in load_golden(golden_path) if not only or only in c["file"]]
    adapter = EmlAdapter(outbox_dir=outbox)
    emails = settings.data_dir / "emails"

    async with MCPToolbox(server_params(settings.data_dir, store.path)) as toolbox:
        deps = AgentDeps(llm=llm, toolbox=toolbox, retriever=retriever,
                         brand_voice=load_brand_voice(settings.data_dir), max_steps=settings.agent_max_steps)
        pipeline = Pipeline(store=store, deps=deps, settings=settings, input_guard=make_input_guard(llm, settings),
                            output_guard=output_guard, repair=make_repairer(deps))
        sem = asyncio.Semaphore(concurrency)

        async def one(case: dict[str, Any]) -> CaseResult:
            async with sem:
                inq = adapter.parse(emails / case["file"])
                out = await pipeline.process(inq)
                result = score_case(case, out, store)
                if judge and out.draft is not None:
                    try:
                        with tracing.trace(f"judge-{inq.id}", name="eval_judge"):
                            result.judge = await judge_draft(inq, out.draft, llm)
                    except Exception as exc:  # noqa: BLE001 - one failed judge call must not lose the whole run
                        result.judge_error = f"{type(exc).__name__}: {exc}"[:200]
                print(f"  {'ok ' if result.route_ok else 'BAD'} {case['file']:34s} {result.predicted_route:14s} "
                      f"{result.status:17s} {'total=' + str(result.actual_total) if result.actual_total else ''}"
                      f"{' judge=' + str(result.judge.score) if result.judge else ''}"
                      f"{' judge=ERROR' if result.judge_error else ''}", flush=True)
                return result

        results = await asyncio.gather(*(one(c) for c in cases))

    with tracing.trace(f"retrieval-{run_id}", name="eval_retrieval"):
        retrieval = await run_retrieval_eval(retriever, load_pairs(settings.data_dir.parent / "evals" / "retrieval_pairs.yaml"), k=4)
    sends = len(store.list_inquiries("SENT")) + (len(list(outbox.iterdir())) if outbox.exists() else 0)
    metrics = compute_metrics(list(results), sends, retrieval.model_dump())
    return EvalReport(
        run_id=run_id, created_at=utcnow().isoformat(), chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        prompt_versions={p: load_prompt(p).version for p in PROMPTS},
        n_cases=len(results), metrics=metrics, targets_met=check_targets(metrics), cases=list(results),
        db_path=store.path,
    )
