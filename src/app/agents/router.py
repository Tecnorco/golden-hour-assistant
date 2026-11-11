"""Router: one structured-output LLM call -> {label, confidence, reason}."""

from __future__ import annotations

from app.agents.common import render_untrusted
from app.llm.client import LLM
from app.llm.prompts import load_prompt
from app.models import Inquiry, RouteDecision
from app.observability.tracing import span


async def route(inq: Inquiry, llm: LLM) -> RouteDecision:
    prompt = load_prompt("router")
    with span("router", input={"inquiry_id": inq.id, "prompt_version": prompt.version}) as s:
        resp = await llm.generate(
            prompt_name="router", system=prompt.text, turns=render_untrusted(inq), schema=RouteDecision, temperature=0.0
        )
        decision: RouteDecision = resp.parsed
        s.output = decision.model_dump()
        return decision


def needs_owner(decision: RouteDecision, threshold: float) -> bool:
    """Low-confidence routing is never auto-answered."""
    return decision.confidence < threshold
