"""Trace timeline and operations report, both read from SQLite.

- render_trace(): the span tree of one inquiry, with latency, tokens, cost,
  tool args/results, guardrail verdicts and errors.
- ops_report(): volume, route mix, escalation rate, owner decisions, cost and
  latency, and the topics we could not answer (the to-do list for new policy docs).
"""

from __future__ import annotations

import json
from collections import Counter

from rich.console import Console
from rich.tree import Tree

from app.models import Decision, TraceSpan
from app.store import Store


def _label(s: TraceSpan, t0: float) -> str:
    offset = (s.start.timestamp() - t0) * 1000
    parts = [f"[bold]{s.name}[/bold]", f"[dim]+{offset:,.0f}ms[/dim]", f"{s.latency_ms or 0:,.0f}ms"]
    if s.tokens_in or s.tokens_out:
        parts.append(f"[cyan]{s.tokens_in}→{s.tokens_out} tok ${s.cost_usd:.5f}[/cyan]")
    if s.error:
        parts.append(f"[red]ERROR {s.error}[/red]")
    return "  ".join(parts)


def render_trace(spans: list[TraceSpan], console: Console | None = None, width: int = 110) -> None:
    console = console or Console()
    if not spans:
        console.print("[yellow]No spans for this id.[/yellow]")
        return
    children: dict[str | None, list[TraceSpan]] = {}
    for s in sorted(spans, key=lambda x: x.start):
        children.setdefault(s.parent_id, []).append(s)
    t0 = min(s.start.timestamp() for s in spans)
    ids = {s.span_id for s in spans}
    roots = [s for s in spans if s.parent_id is None or s.parent_id not in ids]

    def add(node: Tree, s: TraceSpan) -> None:
        branch = node.add(_label(s, t0))
        if s.input_summary:
            branch.add(f"[dim]in:[/dim] {s.input_summary[:width]}")
        if s.output_summary:
            branch.add(f"[dim]out:[/dim] {s.output_summary[:width]}")
        if s.tool_args is not None:
            branch.add(f"[magenta]args:[/magenta] {json.dumps(s.tool_args, default=str)[:width]}")
            branch.add(f"[magenta]result:[/magenta] {json.dumps(s.tool_result, default=str)[:width]}")
        for v in s.verdicts:
            branch.add(f"{'[green]PASS' if v.passed else '[red]FAIL'}[/] {v.name}: {v.details[:width]}")
        for c in children.get(s.span_id, []):
            add(branch, c)

    for r in sorted(roots, key=lambda x: x.start):
        tree = Tree(f"trace [bold]{r.trace_id}[/bold]")
        add(tree, r)
        console.print(tree)


def _pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "-"


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def ops_report(store: Store) -> str:
    rows = store.list_inquiries()
    n = len(rows)
    status = Counter(r["status"] for r in rows)
    routes = Counter(json.loads(r["route"])["label"] for r in rows if r["route"])
    escalated = [r for r in rows if any(t["to_status"] in ("OWNER_QUEUE", "BLOCKED") for t in store.transitions(r["id"]))]
    decisions = [Decision.model_validate_json(d["data"]) for d in store.decisions()]
    actions = Counter(d.action for d in decisions)
    roots = [s for s in store.all_spans() if s.parent_id is None and s.name == "pipeline"]
    lat = [s.latency_ms or 0.0 for s in roots]
    cost = [s.cost_usd for s in roots]

    unanswered: list[str] = []
    needs_info: Counter[str] = Counter()
    for r in rows:
        note = r["owner_note"] or ""
        inq = store.get_inquiry(r["id"])
        if "unanswered topic" in note and inq:
            unanswered.append(inq.subject)
        if r["extracted"] and r["status"] != "BLOCKED":
            draft = store.latest_draft(r["id"])
            if draft and draft.needs_info:
                for t in draft.tool_results:
                    if t.name == "lookup_distance" and isinstance(t.result, dict) and t.result.get("known") is False:
                        needs_info[f"unknown town: {t.args.get('town')}"] += 1
    blocked_reasons = Counter((r["owner_note"] or "").split(":")[0] for r in rows if r["status"] == "BLOCKED")

    lines: list[str] = [
        "# Operations report", "",
        f"**Volume:** {n} inquiries", "",
        "## Status", "", "| Status | Count |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in status.most_common()],
        "", "## Route distribution", "", "| Route | Count | Share |", "|---|---|---|",
        *[f"| {k} | {v} | {_pct(v, n)} |" for k, v in routes.most_common()],
        *([f"| (blocked before routing) | {n - sum(routes.values())} | {_pct(n - sum(routes.values()), n)} |"]
          if n - sum(routes.values()) else []),
        "", "## Escalation", "",
        f"- Escalated (owner queue or blocked): **{len(escalated)} / {n} ({_pct(len(escalated), n)})**",
        *[f"  - blocked: {k or 'unspecified'} × {v}" for k, v in blocked_reasons.items()],
        "", "## Owner decisions", "",
        f"- Decisions: {len(decisions)}",
        f"- Approved as drafted: {actions['approve']} ({_pct(actions['approve'], len(decisions))})",
        f"- Edited then approved: {actions['edit_approve']} ({_pct(actions['edit_approve'], len(decisions))})",
        f"- Rejected: {actions['reject']} ({_pct(actions['reject'], len(decisions))})",
        f"- Sent: {status.get('SENT', 0)}",
        "", "## Cost and latency (per inquiry, pipeline only)", "",
        f"- Average latency: {sum(lat) / len(lat) / 1000:.1f}s · p95 {_p95(lat) / 1000:.1f}s" if lat else "- No pipeline runs yet",
        f"- Average cost: ${sum(cost) / len(cost):.5f} · total ${sum(cost):.4f} (estimates from the price table)" if cost else "",
        "", "## Unanswered topics", "",
        *([f"- {s}" for s in unanswered] or ["- None: every question was covered by a policy document."]),
        *([f"- {k} × {v}" for k, v in needs_info.items()] if needs_info else []),
        "",
    ]
    return "\n".join(line for line in lines if line is not None)

