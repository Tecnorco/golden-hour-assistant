"""Hand-written tool-using agent loop: plan -> act -> observe, repeated.

Each step is one model turn. The model either calls tools (we execute them and
feed results back) or stops. The loop ends when the model calls the terminal
tool (save_draft), or a guard trips:

- step limit: hard cap on model turns;
- loop detection: the same tool with the same arguments twice gets a warning,
  a second repeat aborts;
- prose instead of finishing: one nudge, then abort.

"Owned" arguments (e.g. inquiry_id, tool_results) are filled in by code and
hidden from the model's view of the tool, so the model can never misreport
what the tools returned.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.client import LLM, FunctionResult, ToolSpec, Turn
from app.models import ToolCallRecord
from app.observability.tracing import span
from app.tools import Toolbox

LoopStatus = Literal["finished", "step_limit", "loop_detected", "no_finish"]


class LoopResult(BaseModel):
    status: LoopStatus
    steps: int
    records: list[ToolCallRecord] = Field(default_factory=list)
    final_args: dict[str, Any] | None = None  # arguments of the terminal call, after injection
    final_text: str = ""


def hide_args(spec: ToolSpec, hidden: set[str]) -> ToolSpec:
    params = copy.deepcopy(spec.parameters)
    for name in hidden:
        params.get("properties", {}).pop(name, None)
    if "required" in params:
        params["required"] = [r for r in params["required"] if r not in hidden]
    return spec.model_copy(update={"parameters": params})


def _key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


async def run_tool_loop(
    *,
    llm: LLM,
    prompt_name: str,
    system: str,
    user_message: str,
    toolbox: Toolbox,
    allowed_tools: set[str],
    max_steps: int,
    terminal_tool: str = "save_draft",
    owned_args: Callable[[list[ToolCallRecord]], dict[str, Any]] | None = None,
) -> LoopResult:
    owned_example = owned_args([]) if owned_args else {}
    specs = [
        hide_args(t, set(owned_example)) if t.name == terminal_tool else t
        for t in await toolbox.list_tools()
        if t.name in allowed_tools
    ]
    turns: list[Turn] = [Turn(role="user", text=user_message)]
    records: list[ToolCallRecord] = []
    seen: set[str] = set()
    duplicates = 0
    nudged = False

    with span(f"agent_loop.{prompt_name}", input={"tools": sorted(allowed_tools), "max_steps": max_steps}) as loop_span:
        for step in range(1, max_steps + 1):
            with span(f"step.{step}"):
                resp = await llm.generate(prompt_name=prompt_name, system=system, turns=turns, tools=specs)
            turns.append(resp.turn)

            if not resp.function_calls:
                if nudged:
                    loop_span.output = "no_finish"
                    return LoopResult(status="no_finish", steps=step, records=records, final_text=resp.text)
                nudged = True
                turns.append(Turn(role="user", text=f"Finish by calling {terminal_tool} with the reply draft."))
                continue

            results: list[FunctionResult] = []
            for fc in resp.function_calls:
                if fc.name not in allowed_tools:
                    result: dict[str, Any] = {"ok": False, "error": f"tool '{fc.name}' is not available for this task"}
                    records.append(ToolCallRecord(name=fc.name, args=fc.args, error=result["error"]))
                    results.append(FunctionResult(name=fc.name, response=result, id=fc.id))
                    continue

                key = _key(fc.name, fc.args)
                if key in seen:
                    duplicates += 1
                    if duplicates >= 2:
                        loop_span.output = "loop_detected"
                        return LoopResult(status="loop_detected", steps=step, records=records)
                    result = {"ok": False, "error": "Duplicate call with identical arguments. Use the earlier result."}
                    results.append(FunctionResult(name=fc.name, response=result, id=fc.id))
                    continue
                seen.add(key)

                args = dict(fc.args)
                if fc.name == terminal_tool and owned_args:
                    args.update(owned_args(records))  # code-owned values always win
                with span(f"tool.{fc.name}") as ts:
                    result = await toolbox.call(fc.name, args)
                    ts.tool(args if fc.name != terminal_tool else {k: v for k, v in args.items() if k != "tool_results"}, result)
                records.append(
                    ToolCallRecord(name=fc.name, args=fc.args, result=result, error=None if result.get("ok") else result.get("error"))
                )
                results.append(FunctionResult(name=fc.name, response=result, id=fc.id))

                if fc.name == terminal_tool and result.get("ok"):
                    loop_span.output = f"finished in {step} steps"
                    return LoopResult(status="finished", steps=step, records=records, final_args=args)

            turns.append(Turn(role="tool", function_results=results))

        loop_span.output = "step_limit"
        return LoopResult(status="step_limit", steps=max_steps, records=records)
