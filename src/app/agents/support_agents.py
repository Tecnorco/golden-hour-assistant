"""FAQ agent and reschedule/other agent: policy-grounded replies only.

Both reuse the agent loop with just two tools (search_policies, save_draft).
Code, not the model, enforces the rules that matter:
- FAQ: nothing retrieved above the threshold -> a fixed "I'll check and get back
  to you" holding reply and the owner queue. No improvised answers.
- Every cited chunk id must be one that was actually retrieved.
- Reschedule/other: always flagged for the owner, who confirms dates personally.
"""

from __future__ import annotations

from typing import Any

from app.adapters.eml import first_name
from app.agents.base import AgentDeps, AgentResult, cited_ids, policy_search_tool, retrieved_ids
from app.agents.common import render_untrusted
from app.agents.loop import run_tool_loop
from app.llm.prompts import load_prompt
from app.models import Draft, Inquiry, ToolCallRecord
from app.observability.tracing import current_trace_id, span
from app.tools import CompositeToolbox

POLICY_TOOLS = {"search_policies", "save_draft"}

HOLDING_REPLY = """Hi {name},

Thanks so much for your message! I want to make sure I give you the right answer, so I'm looking into this personally and will get back to you shortly.

Warmly,
Maya
Golden Hour Photography"""


def holding_draft(inq: Inquiry) -> Draft:
    return Draft(
        inquiry_id=inq.id, subject=f"Re: {inq.subject}", body=HOLDING_REPLY.format(name=first_name(inq.sender) or "there"),
        agent_trace_id=current_trace_id(), agent="faq",
    )


def _user_message(inq: Inquiry) -> str:
    name = first_name(inq.sender) or 'unknown (greet with "Hi there,")'
    return (
        f"Customer first name: {name}\n"
        f"Customer subject: {inq.subject}\n"
        f"Today's date: {inq.received_at.date().isoformat()}\n"
        f"{render_untrusted(inq)}"
    )


async def _run_policy_agent(agent: str, inq: Inquiry, deps: AgentDeps) -> tuple[AgentResult, dict[str, Any] | None]:
    prompt = load_prompt(f"{agent}_agent")
    toolbox = CompositeToolbox(deps.toolbox, [policy_search_tool(deps.retriever)])

    def owned(records: list[ToolCallRecord]) -> dict[str, Any]:
        return {"inquiry_id": inq.id, "tool_results": [r.model_dump(mode="json") for r in records], "needs_info": False}

    loop = await run_tool_loop(
        llm=deps.llm, prompt_name=f"{agent}_agent",
        system=f"{prompt.text}\n\n# Brand voice guide\n{deps.brand_voice}",
        user_message=_user_message(inq), toolbox=toolbox, allowed_tools=POLICY_TOOLS,
        max_steps=deps.max_steps, owned_args=owned,
    )
    result = AgentResult(
        agent=agent, status="needs_owner", steps=loop.steps, tool_calls=loop.records,
        retrieved_chunk_ids=retrieved_ids(loop.records),
    )
    if loop.status != "finished" or loop.final_args is None:
        result.reason = f"agent loop ended without a draft: {loop.status}"
        return result, None
    fa = loop.final_args
    result.draft = Draft(
        inquiry_id=inq.id, subject=fa.get("subject", ""), body=fa.get("body", ""),
        sources=list(fa.get("sources") or []), tool_results=loop.records,
        agent_trace_id=current_trace_id(), agent=agent,
    )
    unknown = [c for c in cited_ids(result.draft.body) if c not in result.retrieved_chunk_ids]
    if unknown:
        result.issues.append(f"cites chunk ids that were never retrieved: {unknown}")
    return result, fa


async def run_faq_agent(inq: Inquiry, deps: AgentDeps) -> AgentResult:
    with span("faq_agent") as s:
        result, _ = await _run_policy_agent("faq", inq, deps)
        if result.draft is None:
            pass  # loop failed; already needs_owner with a reason
        elif not result.retrieved_chunk_ids:
            # Nothing relevant in our policies: never let the model improvise an answer.
            result.draft = holding_draft(inq)
            result.status, result.flag_owner = "needs_owner", True
            result.reason = "no policy found above the retrieval threshold (unanswered topic)"
            await deps.toolbox.call("save_draft", {
                "inquiry_id": inq.id, "subject": result.draft.subject, "body": result.draft.body,
                "tool_results": [r.model_dump(mode="json") for r in result.tool_calls],
            })
        else:
            if not cited_ids(result.draft.body):
                result.issues.append("answer states no cited policy")
            if result.issues:
                result.reason = "; ".join(result.issues)
            else:
                result.status = "drafted"
        s.output = {"status": result.status, "reason": result.reason, "cited": result.draft.sources if result.draft else []}
        return result


async def run_other_agent(inq: Inquiry, deps: AgentDeps, label: str) -> AgentResult:
    with span("other_agent", input={"label": label}) as s:
        result, _ = await _run_policy_agent("other", inq, deps)
        result.flag_owner = True  # the owner always confirms reschedules/cancellations personally
        if result.draft is not None and not result.issues:
            result.status = "drafted"
            result.reason = f"{label}: owner must confirm personally; no dates or refunds were promised"
        elif result.issues:
            result.reason = "; ".join(result.issues)
        s.output = {"status": result.status, "reason": result.reason}
        return result
