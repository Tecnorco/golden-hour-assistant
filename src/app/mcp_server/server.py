"""Golden Hour business tools served over MCP (stdio), using the mcp 2.x MCPServer.

Run:  python -m app.mcp_server.server --data-dir data --db var/assistant.db

Security notes:
- This process never loads .env and never needs the Gemini key; the MCP stdio
  client only passes an allowlist of environment variables (PATH, HOME...).
- stdout is the protocol channel. Nothing here may print to stdout.
- Inputs are validated twice: by MCP's schema (from the signature) and by our
  pydantic input models, which also enforce the business rules.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, ValidationError

from mcp.server.mcpserver import MCPServer

from app.models import AddOn
from app.store import Store
from app.tools import (
    DESCRIPTIONS, CalculateQuoteInput, CheckAvailabilityInput, LookupDistanceInput, PackageName, SaveDraftInput,
    ToolContext, validation_error, tool_calculate_quote, tool_check_availability, tool_lookup_distance, tool_save_draft,
)


def build_server(ctx: ToolContext) -> MCPServer:
    mcp = MCPServer(
        name="golden-hour-tools",
        instructions="Business tools for Golden Hour Photography: distances, availability, quotes, draft storage.",
    )

    @mcp.tool(description=DESCRIPTIONS["lookup_distance"])
    def lookup_distance(
        town: Annotated[str, Field(description="Town name only, e.g. 'Pine Harbor' (no venue or street).")],
    ) -> dict[str, Any]:
        return tool_lookup_distance(ctx, LookupDistanceInput(town=town))

    @mcp.tool(description=DESCRIPTIONS["check_availability"])
    def check_availability(
        date: Annotated[str, Field(description="Date to check, ISO format YYYY-MM-DD.")],
    ) -> dict[str, Any]:
        try:
            inp = CheckAvailabilityInput.model_validate({"date": date})
        except ValidationError as exc:
            return validation_error(exc)
        return tool_check_availability(ctx, inp)

    @mcp.tool(description=DESCRIPTIONS["calculate_quote"])
    def calculate_quote(
        package: Annotated[PackageName, Field(description="Package key.")],
        event_date: Annotated[str, Field(description="Date of the shoot, YYYY-MM-DD. Weekends add a surcharge.")],
        distance_miles: Annotated[float, Field(ge=0, description="Miles from the studio, exactly as returned by lookup_distance.")],
        hours: Annotated[float | None, Field(description="Hours of coverage. Required for 'event' only.")] = None,
        add_ons: Annotated[list[AddOn] | None, Field(description="Requested add-ons, each at most once.")] = None,
        extra_photo_sets: Annotated[int, Field(ge=0, description="Number of extra 10-photo sets.")] = 0,
    ) -> dict[str, Any]:
        try:
            inp = CalculateQuoteInput.model_validate({
                "package": package, "event_date": event_date, "distance_miles": distance_miles,
                "hours": hours, "add_ons": add_ons or [], "extra_photo_sets": extra_photo_sets,
            })
        except ValidationError as exc:
            return validation_error(exc)
        return tool_calculate_quote(ctx, inp)

    @mcp.tool(description=DESCRIPTIONS["save_draft"])
    def save_draft(
        inquiry_id: Annotated[str, Field(description="Inquiry id.")],
        subject: Annotated[str, Field(description="Reply subject line.")],
        body: Annotated[str, Field(description="Full reply text, in the owner's voice.")],
        sources: Annotated[list[str] | None, Field(description="Policy chunk ids cited in the body.")] = None,
        tool_results: Annotated[list[dict[str, Any]] | None, Field(description="Tool calls and results used.")] = None,
        needs_info: Annotated[bool, Field(description="True if the reply asks the customer for missing details.")] = False,
    ) -> dict[str, Any]:
        inp = SaveDraftInput(
            inquiry_id=inquiry_id, subject=subject, body=body, sources=sources or [],
            tool_results=tool_results or [], needs_info=needs_info,
        )
        return tool_save_draft(ctx, inp)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Golden Hour MCP tool server (stdio)")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--db", type=Path, help="SQLite file for save_draft (omit = keep drafts in memory)")
    args = parser.parse_args(argv)
    store = Store(args.db) if args.db else None
    ctx = ToolContext.from_data_dir(args.data_dir, save_draft_fn=store.add_draft if store else None)
    build_server(ctx).run(transport="stdio")


if __name__ == "__main__":
    main()
