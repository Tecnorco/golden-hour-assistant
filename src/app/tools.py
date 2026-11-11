"""Business tools: validated inputs, JSON-safe outputs.

These functions are the single implementation behind both the in-process
`LocalToolbox` (used in tests) and the MCP server (Phase 4). Results are always
dicts with `ok: bool`; failures carry an `error` message the agent can read and
act on, instead of an exception that would end the loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.llm.client import ToolSpec
from app.models import AddOn, Draft, ToolCallRecord
from app.pricing.engine import (
    Calendar, PriceList, PricingError, calculate_quote, check_availability, load_calendar, load_price_list,
    lookup_distance,
)

PackageName = Literal["mini_portrait", "standard_portrait", "event", "wedding_half_day", "wedding_full_day"]


# ---------------------------------------------------------------- inputs


class LookupDistanceInput(BaseModel):
    town: str = Field(description="Town name only, e.g. 'Pine Harbor' (no venue or street).")


class CheckAvailabilityInput(BaseModel):
    date: dt.date = Field(description="Date to check, ISO format YYYY-MM-DD.")


class CalculateQuoteInput(BaseModel):
    package: PackageName = Field(description="Package key.")
    event_date: dt.date = Field(description="Date of the shoot, YYYY-MM-DD. Weekends add a surcharge.")
    distance_miles: float = Field(ge=0, description="Miles from the studio, exactly as returned by lookup_distance.")
    hours: float | None = Field(None, description="Hours of coverage. Required for 'event' only.")
    add_ons: list[AddOn] = Field(default_factory=list, description="Requested add-ons, each at most once.")
    extra_photo_sets: int = Field(0, ge=0, description="Number of extra 10-photo sets (only with extra_photos).")


class SaveDraftInput(BaseModel):
    inquiry_id: str = Field(description="Inquiry id.")
    subject: str = Field(description="Reply subject line.")
    body: str = Field(description="Full reply text, in the owner's voice.")
    sources: list[str] = Field(default_factory=list, description="Policy chunk ids cited in the body.")
    tool_results: list[dict[str, Any]] = Field(default_factory=list, description="Tool calls and results used.")
    needs_info: bool = Field(False, description="True if the reply asks the customer for missing details.")


class ListPackagesInput(BaseModel):
    """No arguments."""


class SearchPoliciesInput(BaseModel):
    query: str = Field(description="What policy information you need, as a short question.")


DESCRIPTIONS = {
    "lookup_distance": "Look up driving distance in miles from the studio to a town. Returns known=false for "
    "towns not in the table; never estimate a distance yourself.",
    "check_availability": "Check whether the studio is free on a date. Returns status: available, booked, "
    "blocked (studio closed) or past.",
    "calculate_quote": "Calculate an itemized price quote with weekend surcharge, travel fee, total and deposit. "
    "This is the only source of prices; never do price arithmetic yourself.",
    "list_packages": "List the studio's packages and add-ons (names, what is included, minimum hours). "
    "No prices: quotes always come from calculate_quote.",
    "save_draft": "Save the finished reply draft for the owner to review. Call exactly once, at the end.",
    "search_policies": "Search the studio's policy documents. Returns chunks with chunk_id to cite in [brackets].",
}


# ---------------------------------------------------------------- context + implementations


@dataclass
class ToolContext:
    price_list: PriceList
    calendar: Calendar
    save_draft_fn: Callable[[Draft], None] | None = None
    today: dt.date | None = None
    saved: list[Draft] = field(default_factory=list)

    @classmethod
    def from_data_dir(cls, data_dir: Path, **kwargs: Any) -> "ToolContext":
        return cls(load_price_list(data_dir / "pricing.yaml"), load_calendar(data_dir / "calendar.json"), **kwargs)


def tool_lookup_distance(ctx: ToolContext, inp: LookupDistanceInput) -> dict[str, Any]:
    miles = lookup_distance(ctx.price_list, inp.town)
    if miles is None:
        return {"ok": True, "town": inp.town, "known": False,
                "note": "Town not in our distance table. Ask the customer for the town or venue address."}
    return {"ok": True, "town": inp.town, "known": True, "distance_miles": miles}


def tool_check_availability(ctx: ToolContext, inp: CheckAvailabilityInput) -> dict[str, Any]:
    status = check_availability(ctx.calendar, inp.date, today=ctx.today)
    return {"ok": True, "date": inp.date.isoformat(), "weekday": inp.date.strftime("%A"), "status": status}


def tool_calculate_quote(ctx: ToolContext, inp: CalculateQuoteInput) -> dict[str, Any]:
    try:
        q = calculate_quote(
            ctx.price_list, inp.package, inp.event_date, inp.distance_miles,
            hours=inp.hours, add_ons=list(inp.add_ons), extra_photo_sets=inp.extra_photo_sets,
        )
    except PricingError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **q.model_dump(mode="json")}


def tool_list_packages(ctx: ToolContext, inp: ListPackagesInput) -> dict[str, Any]:
    pl = ctx.price_list
    return {
        "ok": True,
        "packages": [
            {"key": k, "name": p.name, "includes": p.includes, **({"min_hours": p.min_hours} if p.min_hours else {})}
            for k, p in pl.packages.items()
        ],
        "add_ons": [{"key": k, "name": a.name, **({"only_for": a.allowed_for} if a.allowed_for else {})}
                    for k, a in pl.add_ons.items()],
    }


def tool_save_draft(ctx: ToolContext, inp: SaveDraftInput) -> dict[str, Any]:
    draft = Draft(
        inquiry_id=inp.inquiry_id,
        subject=inp.subject,
        body=inp.body,
        sources=inp.sources,
        tool_results=[ToolCallRecord.model_validate(r) for r in inp.tool_results],
        needs_info=inp.needs_info,
    )
    if ctx.save_draft_fn is not None:
        ctx.save_draft_fn(draft)
    ctx.saved.append(draft)
    return {"ok": True, "saved": True, "inquiry_id": inp.inquiry_id}


BUSINESS_TOOLS: dict[str, tuple[type[BaseModel], Callable[[ToolContext, Any], dict[str, Any]]]] = {
    "lookup_distance": (LookupDistanceInput, tool_lookup_distance),
    "check_availability": (CheckAvailabilityInput, tool_check_availability),
    "calculate_quote": (CalculateQuoteInput, tool_calculate_quote),
    "list_packages": (ListPackagesInput, tool_list_packages),
    "save_draft": (SaveDraftInput, tool_save_draft),
}


def tool_spec(name: str, model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=DESCRIPTIONS[name], parameters=model.model_json_schema())


# ---------------------------------------------------------------- toolbox interface


class Toolbox(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...
    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


def validation_error(exc: ValidationError) -> dict[str, Any]:
    problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
    return {"ok": False, "error": f"invalid arguments: {problems}"}


class LocalToolbox:
    """In-process tools. Used by unit tests; the app uses the MCP toolbox."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def list_tools(self) -> list[ToolSpec]:
        return [tool_spec(n, m) for n, (m, _) in BUSINESS_TOOLS.items()]

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in BUSINESS_TOOLS:
            return {"ok": False, "error": f"unknown tool '{name}'"}
        model, fn = BUSINESS_TOOLS[name]
        try:
            inp = model.model_validate(args)
        except ValidationError as exc:
            return validation_error(exc)
        return fn(self.ctx, inp)


class FunctionTool:
    """An agent-local tool (not a business tool), e.g. policy search over RAG."""

    def __init__(self, name: str, model: type[BaseModel], fn: Callable[[Any], Awaitable[dict[str, Any]]]) -> None:
        self.spec = tool_spec(name, model)
        self.model = model
        self.fn = fn


class CompositeToolbox:
    """Business toolbox (local or MCP) plus agent-local tools, behind one interface."""

    def __init__(self, base: Toolbox, local: list[FunctionTool]) -> None:
        self.base = base
        self.local = {t.spec.name: t for t in local}

    async def list_tools(self) -> list[ToolSpec]:
        return [*(await self.base.list_tools()), *(t.spec for t in self.local.values())]

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self.local.get(name)
        if tool is None:
            return await self.base.call(name, args)
        try:
            inp = tool.model.model_validate(args)
        except ValidationError as exc:
            return validation_error(exc)
        return await tool.fn(inp)
