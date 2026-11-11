"""Proves the MCP server works with a plain, generic MCP client.

Uses only the official `mcp` SDK (no app code): spawn the server over stdio,
list its tools, call each one, print the results.

Run:  python -m app.mcp_server.demo_client
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[3]


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server", "--data-dir", str(ROOT / "data")],  # no --db: drafts stay in memory
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print(f"Connected to {init.server_info.name}\n")
        for tool in (await session.list_tools()).tools:
            print(f"- {tool.name}: {tool.description}")
            print(f"  params: {sorted(tool.input_schema.get('properties', {}))}")

        calls = [
            ("lookup_distance", {"town": "Lakeview Springs"}),
            ("lookup_distance", {"town": "Glenmoor"}),
            ("check_availability", {"date": "2027-02-14"}),
            ("calculate_quote", {"package": "wedding_full_day", "event_date": "2027-06-12", "distance_miles": 48,
                                 "add_ons": ["second_photographer", "printed_album", "engagement_session"]}),
            ("calculate_quote", {"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 0,
                                 "add_ons": ["second_photographer"]}),
            ("save_draft", {"inquiry_id": "demo_1", "subject": "Re: hello", "body": "Hi there!"}),
        ]
        for name, args in calls:
            result = await session.call_tool(name, args)
            payload = result.structured_content or [getattr(c, "text", "") for c in result.content]
            print(f"\n>>> {name}({json.dumps(args)})\n{json.dumps(payload, indent=1)[:900]}")


if __name__ == "__main__":
    asyncio.run(main())
