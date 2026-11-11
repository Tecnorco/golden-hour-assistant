"""Pieces shared by the specialist agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.client import LLM
from app.models import Draft, ToolCallRecord
from app.rag.retriever import Retriever
from app.tools import FunctionTool, SearchPoliciesInput, Toolbox

CITATION = re.compile(r"\[([a-z0-9-]+#[a-z0-9-]+#\d+)\]")


@dataclass
class AgentDeps:
    llm: LLM
    toolbox: Toolbox  # business tools: MCP in the app, local in some tests
    retriever: Retriever | None
    brand_voice: str
    max_steps: int = 8


class AgentResult(BaseModel):
    agent: str
    status: Literal["drafted", "needs_owner"]
    reason: str = ""
    draft: Draft | None = None
    flag_owner: bool = False  # drafted, but the owner must act personally (e.g. reschedules)
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


def retrieved_ids(records: list[ToolCallRecord]) -> list[str]:
    return sorted(
        {c["chunk_id"] for r in records if r.name == "search_policies" and isinstance(r.result, dict)
         for c in r.result.get("chunks", [])}
    )


def cited_ids(text: str) -> list[str]:
    return list(dict.fromkeys(CITATION.findall(text)))
