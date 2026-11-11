"""One automatic repair attempt for drafts that fail output guardrails.

The repair call sees the failed checks, the draft and the verified facts (tool
results, retrieved chunks). It does NOT see the customer email, which keeps
injected text out of this step. The repaired draft is re-checked by the
orchestrator; a second failure goes to the owner queue.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.agents.base import AgentDeps, AgentResult
from app.llm.prompts import load_prompt
from app.models import Draft, Inquiry
from app.orchestrator import OutputCheck, Repairer
from app.observability.tracing import span


class RepairedDraft(BaseModel):
    subject: str
    body: str
    sources: list[str] = Field(default_factory=list)


def _facts(result: AgentResult) -> str:
    facts = []
    for r in result.tool_calls:
        if r.name in ("save_draft",) or not isinstance(r.result, dict):
            continue
        if r.name == "search_policies":
            for c in r.result.get("chunks", []):
                facts.append(f"POLICY [{c['chunk_id']}]: {c['text']}")
        else:
            facts.append(f"TOOL {r.name}({json.dumps(r.args, default=str)}) -> {json.dumps(r.result, default=str)}")
    return "\n\n".join(facts) or "(no verified facts)"


def make_repairer(deps: AgentDeps) -> Repairer:
    async def repair(inq: Inquiry, draft: Draft, result: AgentResult, check: OutputCheck) -> AgentResult | None:
        prompt = load_prompt("repair")
        failed = "\n".join(f"- {v.name}: {v.details}" for v in check.verdicts if not v.passed)
        message = (
            f"FAILED CHECKS:\n{failed}\n\nVERIFIED FACTS:\n{_facts(result)}\n\n"
            f"DRAFT SUBJECT: {draft.subject}\nDRAFT BODY:\n{draft.body}"
        )
        with span("repair_draft", input={"failed": [v.name for v in check.verdicts if not v.passed]}) as s:
            resp = await deps.llm.generate(
                prompt_name="repair", system=f"{prompt.text}\n\n# Brand voice guide\n{deps.brand_voice}",
                turns=message, schema=RepairedDraft, temperature=0.0,
            )
            fixed: RepairedDraft = resp.parsed
            new_draft = draft.model_copy(update={"subject": fixed.subject, "body": fixed.body, "sources": fixed.sources})
            saved = await deps.toolbox.call("save_draft", {
                "inquiry_id": inq.id, "subject": new_draft.subject, "body": new_draft.body,
                "sources": new_draft.sources, "needs_info": new_draft.needs_info,
                "tool_results": [r.model_dump(mode="json") for r in result.tool_calls],
            })
            s.output = {"saved": saved.get("ok")}
            return result.model_copy(update={"draft": new_draft})

    return repair
