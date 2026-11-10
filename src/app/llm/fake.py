"""Deterministic stand-in for GeminiLLM so `make test` needs no API key.

Responses are queued per prompt name. Each entry is either:
- a dict / pydantic model  -> returned as structured output (JSON text + parsed)
- a str                    -> returned as plain text
- a FunctionCall or list[FunctionCall] -> returned as tool calls
- a callable(turns) -> any of the above, for responses that depend on input
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.llm.client import FunctionCall, LLMResponse, ToolSpec, Turn, Usage, parse_structured
from app.observability.tracing import record_llm_usage, span

Scripted = Any


class FakeLLM:
    def __init__(self, scripts: dict[str, list[Scripted] | Callable[..., Scripted]] | None = None) -> None:
        self.scripts: dict[str, list[Scripted] | Callable[..., Scripted]] = dict(scripts or {})
        self.calls: list[dict[str, Any]] = []
        self._cursor: dict[str, int] = defaultdict(int)

    def _next(self, prompt_name: str, turns: list[Turn]) -> Scripted:
        script = self.scripts.get(prompt_name)
        if script is None:
            raise KeyError(f"FakeLLM has no script for prompt '{prompt_name}'")
        if callable(script):
            return script(turns)
        i = self._cursor[prompt_name]
        self._cursor[prompt_name] += 1
        item = script[min(i, len(script) - 1)]  # repeat the last entry if over-asked
        return item(turns) if callable(item) else item

    async def generate(
        self,
        *,
        prompt_name: str,
        system: str,
        turns: list[Turn] | str,
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if isinstance(turns, str):
            turns = [Turn(role="user", text=turns)]
        self.calls.append({"prompt": prompt_name, "system": system, "turns": turns, "tools": tools})
        with span(f"llm.{prompt_name}", input={"model": "fake", "turns": len(turns)}) as sp:
            item = self._next(prompt_name, turns)
            record_llm_usage(10, 5, 0.0, model="fake", prompt=prompt_name)
            usage = Usage(tokens_in=10, tokens_out=5)
            if isinstance(item, FunctionCall):
                item = [item]
            if isinstance(item, list) and item and isinstance(item[0], FunctionCall):
                sp.output = item
                return LLMResponse(turn=Turn(role="model", function_calls=item), function_calls=item, usage=usage, model="fake")
            if isinstance(item, BaseModel):
                item = item.model_dump(mode="json")
            text = item if isinstance(item, str) else json.dumps(item)
            parsed = parse_structured(text, schema) if schema is not None else None
            sp.output = text
            return LLMResponse(text=text, parsed=parsed, turn=Turn(role="model", text=text), usage=usage, model="fake")

    async def embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        raise NotImplementedError("use FakeEmbedder in RAG tests")
