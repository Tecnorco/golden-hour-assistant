"""Markdown rendering for eval reports, and --compare between two runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.evaluation.golden import TARGETS, EvalReport

METRIC_LABELS = [
    ("routing_accuracy", "Routing accuracy", "pct"),
    ("extraction_accuracy", "Extraction accuracy (all fields)", "pct"),
    ("extraction_service_type", "  - service_type", "pct"),
    ("extraction_event_date", "  - event_date", "pct"),
    ("extraction_location", "  - location", "pct"),
    ("extraction_hours", "  - hours", "pct"),
    ("extraction_add_ons", "  - add_ons", "pct"),
    ("extraction_extra_photo_sets", "  - extra_photo_sets", "pct"),
    ("quote_correct", "Quote correctness (total + deposit + shown in draft)", "raw"),
    ("availability_accuracy", "Availability reported correctly", "pct"),
    ("clarification_precision", "Clarification precision", "pct"),
    ("clarification_recall", "Clarification recall", "pct"),
    ("clarification_fields_exact", "Asked for exactly the missing details", "pct"),
    ("injection_blocked_rate", "Injection blocked rate", "pct"),
    ("false_block_rate", "Legitimate emails wrongly blocked", "pct"),
    ("unapproved_sends", "Unapproved sends", "raw"),
    ("groundedness_rate", "Groundedness (judge score >= 4)", "pct"),
    ("groundedness_mean_score", "Groundedness mean score (1-5)", "raw"),
    ("addresses_request_rate", "Draft addresses the request (judge)", "pct"),
    ("judge_errors", "Cases not judged (judge call failed)", "raw"),
    ("retrieval_hit_rate", "Retrieval hit rate @4", "pct"),
    ("retrieval_mrr", "Retrieval MRR @4", "raw"),
    ("owner_queue_rate", "Sent to owner queue", "pct"),
    ("avg_latency_s", "Average latency per inquiry (s)", "raw"),
    ("avg_cost_usd", "Average cost per inquiry (USD, est.)", "raw"),
    ("total_cost_usd", "Total pipeline cost (USD, est.)", "raw"),
]


def fmt(value: Any, kind: str) -> str:
    if value is None:
        return "-"
    return f"{value:.0%}" if kind == "pct" else str(value)


def to_markdown(r: EvalReport) -> str:
    lines = [
        f"# Eval report `{r.run_id}`",
        "",
        f"{r.n_cases} golden emails · model `{r.chat_model}` · embeddings `{r.embedding_model}` · {r.created_at[:19]}Z",
        "",
        "Prompt versions: " + ", ".join(f"{k} v{v}" for k, v in r.prompt_versions.items()),
        "",
        "## Metrics",
        "",
        "| Metric | Value | Target |",
        "|---|---|---|",
    ]
    for key, label, kind in METRIC_LABELS:
        target = ""
        if key in TARGETS or (key == "quote_correct" and "quote_correct_rate" in TARGETS):
            tkey = "quote_correct_rate" if key == "quote_correct" else key
            op, val = TARGETS[tkey]
            met = r.targets_met.get(tkey)
            target = f"{'✅' if met else '❌'} {op} {fmt(val, 'pct') if isinstance(val, float) else val}"
        lines.append(f"| {label} | {fmt(r.metrics.get(key), kind)} | {target} |")

    lines += [
        "",
        "## Cases",
        "",
        "| File | Expected | Predicted | Status | Total (exp / got) | Clarify (exp / got) | Judge | Latency | Cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in r.cases:
        total = f"{c.expected_total} / {c.actual_total}" if c.expected_total is not None else ""
        if c.quote_ok is False:
            total = f"**{total}** ❌"
        clar = f"{c.expected_clarification} / {c.needs_info}" if c.expected_clarification is not None else ""
        lines.append(
            f"| {c.file} | {c.expected_route} | {'' if c.route_ok else '❌ '}{c.predicted_route} | {c.status} | {total} | "
            f"{clar} | {c.judge.score if c.judge else ('error' if c.judge_error else '-')} | {c.latency_ms / 1000:.1f}s | ${c.cost_usd:.4f} |"
        )

    problems = []
    for c in r.cases:
        issues = list(c.field_mismatches)
        if not c.route_ok:
            issues.append(f"route: expected {c.expected_route}, got {c.predicted_route} ({c.reason})")
        if c.quote_ok is False:
            issues.append(f"quote: expected {c.expected_total}/{c.expected_deposit}, got {c.actual_total}/{c.actual_deposit}, "
                          f"total shown in draft: {c.total_in_draft}")
        if c.expected_missing and sorted(c.expected_missing) != sorted(c.actual_missing):
            issues.append(f"missing details: expected {c.expected_missing}, got {c.actual_missing}")
        if c.status == "OWNER_QUEUE":
            issues.append(f"owner queue: {c.reason}")
        if c.judge_error:
            issues.append(f"not judged: {c.judge_error}")
        if c.judge and (not c.judge.grounded or c.judge.unsupported_claims):
            issues.append(f"judge {c.judge.score}/5: {c.judge.unsupported_claims} - {c.judge.reasoning}")
        if issues:
            problems.append(f"- **{c.file}** (`{c.inquiry_id}`)\n" + "\n".join(f"  - {i}" for i in issues))
    lines += ["", "## Findings", ""] + (problems or ["No issues found."])
    lines += ["", f"Traces: `python -m app.cli trace <inquiry_id> --db {r.db_path}`", ""]
    return "\n".join(lines)


LOWER_IS_BETTER = {"judge_errors", "unapproved_sends", "false_block_rate", "owner_queue_rate", "avg_latency_s", "avg_cost_usd", "total_cost_usd"}
NOISY = {"avg_latency_s", "avg_cost_usd", "total_cost_usd"}


def compare(a: EvalReport, b: EvalReport) -> str:
    """Markdown diff: B relative to A (A = baseline)."""
    lines = [f"# Eval comparison: `{a.run_id}` → `{b.run_id}`", ""]
    changed_prompts = {k: (a.prompt_versions.get(k), v) for k, v in b.prompt_versions.items() if a.prompt_versions.get(k) != v}
    if changed_prompts:
        lines.append("Prompt changes: " + ", ".join(f"{k} v{x}→v{y}" for k, (x, y) in changed_prompts.items()))
        lines.append("")
    if a.chat_model != b.chat_model:
        lines += [f"Model change: `{a.chat_model}` → `{b.chat_model}`", ""]
    lines += ["| Metric | Baseline | New | Change |", "|---|---|---|---|"]
    for key, label, kind in METRIC_LABELS:
        va, vb = a.metrics.get(key), b.metrics.get(key)
        delta = ""
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va != vb:
            d = vb - va
            delta = f"{d:+.0%}" if kind == "pct" else f"{d:+.4g}"
            if key in NOISY and va and abs(d) / abs(va) < 0.10:
                delta += " (noise, <10%)"  # LLM latency and token counts wobble run to run
            else:
                worse = d > 0 if key in LOWER_IS_BETTER else d < 0
                delta += " ⚠️ regression" if worse else " ✅"
        elif va != vb:
            delta = "changed"
        lines.append(f"| {label} | {fmt(va, kind)} | {fmt(vb, kind)} | {delta} |")

    by_file = {c.file: c for c in a.cases}
    changes = []
    for cb in b.cases:
        ca = by_file.get(cb.file)
        if ca is None:
            continue
        diffs = []
        if ca.predicted_route != cb.predicted_route:
            diffs.append(f"route {ca.predicted_route} → {cb.predicted_route}")
        if ca.status != cb.status:
            diffs.append(f"status {ca.status} → {cb.status}")
        if ca.quote_ok != cb.quote_ok:
            diffs.append(f"quote_ok {ca.quote_ok} → {cb.quote_ok}")
        if ca.needs_info != cb.needs_info:
            diffs.append(f"needs_info {ca.needs_info} → {cb.needs_info}")
        if sorted(k for k, v in ca.field_results.items() if not v) != sorted(k for k, v in cb.field_results.items() if not v):
            diffs.append(f"wrong fields {[k for k, v in ca.field_results.items() if not v]} → "
                         f"{[k for k, v in cb.field_results.items() if not v]}")
        sa, sb = (ca.judge.score if ca.judge else None), (cb.judge.score if cb.judge else None)
        if sa != sb:
            diffs.append(f"judge {sa} → {sb}")
        if diffs:
            changes.append(f"- {cb.file}: " + "; ".join(diffs))
    lines += ["", "## Per-case changes", ""] + (changes or ["No per-case changes."])
    return "\n".join(lines) + "\n"


def latest_reports(reports_dir: Path, n: int = 2) -> list[Path]:
    return sorted(reports_dir.glob("eval-*.json"))[-n:]


def load_report(path: Path) -> EvalReport:
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))
