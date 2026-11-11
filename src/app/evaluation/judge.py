"""LLM-as-judge for groundedness, with a written rubric (prompts/judge.md)."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.agents.common import render_untrusted
from app.llm.client import LLM
from app.llm.prompts import load_prompt
from app.models import Draft, Inquiry
from app.observability.tracing import span


class JudgeVerdict(BaseModel):
    score: int = Field(ge=1, le=5)
    unsupported_claims: list[str] = Field(default_factory=list)
    addresses_request: bool
    reasoning: str

    @property
    def grounded(self) -> bool:
        return self.score >= 4


def evidence_text(draft: Draft) -> str:
    parts = []
    for t in draft.tool_results:
        if t.name == "save_draft" or not isinstance(t.result, dict):
            continue
        if t.name == "search_policies":
            for c in t.result.get("chunks", []):
                parts.append(f"POLICY [{c['chunk_id']}]: {c['text']}")
        else:
            parts.append(f"TOOL {t.name}({json.dumps(t.args, default=str)}) -> {json.dumps(t.result, default=str)}")
    return "\n\n".join(parts) or "(no tool results or policy excerpts)"


async def judge_draft(inq: Inquiry, draft: Draft, llm: LLM) -> JudgeVerdict:
    prompt = load_prompt("judge")
    message = f"{render_untrusted(inq)}\n\nEVIDENCE:\n{evidence_text(draft)}\n\nDRAFT REPLY:\n{draft.body}"
    with span("judge", input={"inquiry_id": inq.id, "prompt_version": prompt.version}) as s:
        resp = await llm.generate(prompt_name="judge", system=prompt.text, turns=message, schema=JudgeVerdict, temperature=0.0)
        verdict: JudgeVerdict = resp.parsed
        s.output = {"score": verdict.score, "unsupported": len(verdict.unsupported_claims)}
        return verdict
