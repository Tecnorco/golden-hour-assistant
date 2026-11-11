"""MCP client toolbox: the agents' only route to business tools in the app.

Starts the MCP server as a subprocess over stdio, converts MCP tool schemas into
our ToolSpec (which becomes a Gemini FunctionDeclaration), and calls tools over
the protocol. Implements the same `Toolbox` interface as LocalToolbox.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.llm.client import ToolSpec


def server_params(data_dir: Path, db_path: Path | None) -> StdioServerParameters:
    args = ["-m", "app.mcp_server.server", "--data-dir", str(data_dir)]
    if db_path is not None:
        args += ["--db", str(db_path)]
    # env=None: the SDK passes only its safe allowlist (PATH, HOME, ...), so the
    # Gemini key and other secrets in our environment never reach the server.
    return StdioServerParameters(command=sys.executable, args=args, env=None)


def mcp_tool_to_spec(tool: Any) -> ToolSpec:
    """MCP input schemas are JSON Schema; Gemini accepts JSON Schema directly
    (FunctionDeclaration.parameters_json_schema). Drop the cosmetic 'title' keys."""
    schema = json.loads(json.dumps(tool.input_schema))
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return ToolSpec(name=tool.name, description=tool.description or "", parameters=schema)


def parse_result(result: Any) -> dict[str, Any]:
    if result.structured_content is not None:
        data = result.structured_content
        # Tools returning a dict may be wrapped as {"result": {...}} by the server.
        if set(data) == {"result"} and isinstance(data["result"], dict):
            data = data["result"]
    else:
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"ok": not result.is_error, "text": text}
    if result.is_error:
        msg = data.get("text") if isinstance(data, dict) else str(data)
        return {"ok": False, "error": msg or "tool error"}
    return data if isinstance(data, dict) else {"ok": True, "result": data}


def _server_errlog(fallback: Path | None) -> TextIO:
    """Where the server's stderr goes. Normally our stderr (useful when debugging);
    when stderr has no real file handle (test capture, some hosts), a log file."""
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, io.UnsupportedOperation, ValueError):
        path = fallback or Path(tempfile.gettempdir()) / "golden-hour-mcp-server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed by MCPToolbox.__aexit__


class MCPToolbox:
    """Use as `async with MCPToolbox(params) as tb:`; one server process per block."""

    def __init__(self, params: StdioServerParameters, errlog_path: Path | None = None) -> None:
        self.params = params
        self.errlog_path = errlog_path
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self._specs: list[ToolSpec] | None = None

    async def __aenter__(self) -> "MCPToolbox":
        self._stack = AsyncExitStack()
        errlog = _server_errlog(self.errlog_path)
        if errlog is not sys.stderr:
            self._stack.callback(errlog.close)
        read, write = await self._stack.enter_async_context(stdio_client(self.params, errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self.session = None

    async def list_tools(self) -> list[ToolSpec]:
        assert self.session is not None, "MCPToolbox used outside 'async with'"
        if self._specs is None:
            listed = await self.session.list_tools()
            self._specs = [mcp_tool_to_spec(t) for t in listed.tools]
        return self._specs

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.session is not None, "MCPToolbox used outside 'async with'"
        result = await self.session.call_tool(name, args)
        return parse_result(result)
