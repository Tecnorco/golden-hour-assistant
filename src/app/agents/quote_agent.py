"""Quote agent: missing-info check -> tool loop -> deterministic verification.

The LLM plans and writes; code decides the facts:
- which fields are missing (missing_quote_fields), so we never quote on guesses;
- needs_info (missing fields, unknown town, or no successful quote);
- that the calculate_quote arguments match the extracted fields and the looked-up
  distance exactly (verify_quote_calls). A mismatch goes to the owner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.adapters.eml import first_name
from app.agents.common import render_untrusted
from app.agents.loop import LoopResult, run_tool_loop
from app.llm.client import LLM
from app.llm.prompts import load_prompt
from app.models import Draft, ExtractedFields, Inquiry, ToolCallRecord, missing_quote_fields
from app.observability.tracing import current_trace_id, span
from app.rag.retriever import Retriever
from app.tools import CompositeToolbox, FunctionTool, SearchPoliciesInput, Toolbox

MISSING_LABELS = {
    "service_type": "which service they would like (mini or standard portrait session, event coverage, "
    "or a half-day or full-day wedding)",
    "event_date": "the date of the shoot or event",
    "location": "the town or venue where it will take place",
    "hours": "how many hours of event coverage they need (2-hour minimum)",
}

QUOTE_TOOLS = {"lookup_distance", "check_availability", "calculate_quote", "search_policies", "save_draft"}


@dataclass
class AgentDeps:
    llm: LLM
    toolbox: Toolbox  # business tools (MCP in the app, local in tests)
    retriever: Retriever | None
    brand_voice: str
    max_steps: int = 8


class AgentResult(BaseModel):
    agent: str
    status: Literal["drafted", "needs_owner"]
    reason: str = ""
    draft: Draft | None = None
    missing: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    steps: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    retrieved_chunk_ids: list[str] = Field(default_factory=list)


def load_brand_voice(data_dir: Path) -> str:
    return (data_dir / "docs" / "brand-voice.md").read_text(encoding="utf-8")


def policy_search_tool(retriever: Retriever | None) -> FunctionTool:
    async def search(inp: SearchPoliciesInput) -> dict[str, Any]:
        if retriever is None:
            return {"ok": True, "chunks": [], "note": "policy search unavailable"}
        hits = await retriever.retrieve(inp.query)
        if not hits:
            return {"ok": True, "chunks": [], "note": "No relevant policy found. Do not state this policy."}
        return {
            "ok": True,
            "chunks": [{"chunk_id": h.chunk_id, "heading": h.heading, "text": h.text, "score": h.score} for h in hits],
        }

    return FunctionTool("search_policies", SearchPoliciesInput, search)


def _last_ok(records: list[ToolCallRecord], name: str) -> ToolCallRecord | None:
    for r in reversed(records):
        if r.name == name and r.error is None and isinstance(r.result, dict) and r.result.get("ok"):
            return r
    return None


def compute_needs_info(missing: list[str], records: list[ToolCallRecord]) -> bool:
    if missing:
        return True
    lookup = _last_ok(records, "lookup_distance")
    if lookup is not None and not lookup.result.get("known"):
        return True
    return _last_ok(records, "calculate_quote") is None


def verify_quote_calls(fields: ExtractedFields, records: list[ToolCallRecord]) -> list[str]:
    """Deterministic cross-check: the quote must be computed from the customer's
    details and the looked-up distance, never from numbers the model chose."""
    issues: list[str] = []
    quote = _last_ok(records, "calculate_quote")
    if quote is None:
        return ["calculate_quote was never called successfully"]
    a = quote.args
    lookup = _last_ok(records, "lookup_distance")
    if lookup is None or not lookup.result.get("known"):
        issues.append("quote calculated without a known distance from lookup_distance")
    elif float(a.get("distance_miles", -1)) != float(lookup.result["distance_miles"]):
        issues.append(f"distance_miles {a.get('distance_miles')} != looked-up {lookup.result['distance_miles']}")
    if lookup is not None and fields.location and lookup.args.get("town", "").lower() != fields.location.lower():
        issues.append(f"distance looked up for '{lookup.args.get('town')}' but customer said '{fields.location}'")
    if a.get("package") != fields.service_type:
        issues.append(f"package {a.get('package')} != requested {fields.service_type}")
    if str(a.get("event_date")) != (fields.event_date.isoformat() if fields.event_date else None):
        issues.append(f"event_date {a.get('event_date')} != requested {fields.event_date}")
    if fields.service_type == "event" and float(a.get("hours") or 0) != float(fields.hours or 0):
        issues.append(f"hours {a.get('hours')} != requested {fields.hours}")
    if sorted(set(a.get("add_ons") or [])) != sorted(set(fields.add_ons)):
        issues.append(f"add_ons {a.get('add_ons')} != requested {fields.add_ons}")
    if int(a.get("extra_photo_sets") or 0) != fields.extra_photo_sets:
        issues.append(f"extra_photo_sets {a.get('extra_photo_sets')} != requested {fields.extra_photo_sets}")
    avail = _last_ok(records, "check_availability")
    if avail is None or avail.args.get("date") != a.get("event_date"):
        issues.append("availability was not checked for the quoted date")
    return issues


def build_user_message(inq: Inquiry, fields: ExtractedFields, missing: list[str]) -> str:
    name = first_name(inq.sender) or 'unknown (greet with "Hi there,")'
    parts = [
        f"Customer first name: {name}",
        f"Customer subject: {inq.subject}",
        f"Today's date: {inq.received_at.date().isoformat()}",
        "Extracted details (from our extraction step):",
        json.dumps(fields.model_dump(mode="json"), indent=1),
    ]
    if missing:
        parts.append("MISSING DETAILS - ask the customer for all of these, do not quote:")
        parts += [f"- {MISSING_LABELS.get(m, m)}" for m in missing]
    else:
        parts.append("All required details are present. Follow the full quote process.")
    parts.append(render_untrusted(inq))
    return "\n".join(parts)


async def run_quote_agent(inq: Inquiry, fields: ExtractedFields, deps: AgentDeps) -> AgentResult:
    prompt = load_prompt("quote_agent")
    missing = missing_quote_fields(fields)
    with span("quote_agent", input={"missing": missing, "prompt_version": prompt.version}) as s:
        toolbox = CompositeToolbox(deps.toolbox, [policy_search_tool(deps.retriever)])
        allowed = {"save_draft"} if missing else QUOTE_TOOLS

        def owned(records: list[ToolCallRecord]) -> dict[str, Any]:
            return {
                "inquiry_id": inq.id,
                "tool_results": [r.model_dump(mode="json") for r in records],
                "needs_info": compute_needs_info(missing, records),
            }

        loop: LoopResult = await run_tool_loop(
            llm=deps.llm,
            prompt_name="quote_agent",
            system=f"{prompt.text}\n\n# Brand voice guide\n{deps.brand_voice}",
            user_message=build_user_message(inq, fields, missing),
            toolbox=toolbox,
            allowed_tools=allowed,
            max_steps=deps.max_steps,
            owned_args=owned,
        )
        retrieved = sorted(
            {c["chunk_id"] for r in loop.records if r.name == "search_policies" and isinstance(r.result, dict)
             for c in r.result.get("chunks", [])}
        )
        result = AgentResult(
            agent="quote", status="needs_owner", missing=missing, steps=loop.steps,
            tool_calls=loop.records, retrieved_chunk_ids=retrieved,
        )
        if loop.status != "finished" or loop.final_args is None:
            result.reason = f"agent loop ended without a draft: {loop.status}"
            s.output = result.reason
            return result

        fa = loop.final_args
        result.draft = Draft(
            inquiry_id=inq.id, subject=fa.get("subject", ""), body=fa.get("body", ""),
            sources=list(fa.get("sources") or []), tool_results=loop.records,
            agent_trace_id=current_trace_id(), needs_info=bool(fa.get("needs_info")), agent="quote",
        )
        if not result.draft.needs_info:
            result.issues = verify_quote_calls(fields, loop.records)
        if result.issues:
            result.reason = "quote verification failed"
        else:
            result.status = "drafted"
        s.output = {"status": result.status, "needs_info": result.draft.needs_info, "issues": result.issues}
        return result
