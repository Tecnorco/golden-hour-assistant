"""MCP client toolbox: the agents' only route to business tools in the app.

Starts the MCP server as a subprocess over stdio, converts MCP tool schemas into
our ToolSpec (which becomes a Gemini FunctionDeclaration), and calls tools over
the protocol. Implements the same `Toolbox` interface as LocalToolbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, TextIO

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.llm.client import ToolSpec
from app.observability.tracing import span


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
    """Use as `async with MCPToolbox(params) as tb:`.

    The server connection lives in its own background task: the MCP SDK's
    anyio task group must be entered and exited in the same task, and tool
    calls arrive from many tasks (the eval runs inquiries concurrently).

    If the server process dies, the next tool call restarts it once and
    retries. Tool-level failures never trigger this: tools report those as
    data ({"ok": False, ...}); only a broken connection raises.
    """

    def __init__(self, params: StdioServerParameters, errlog_path: Path | None = None, max_restarts: int = 3) -> None:
        self.params = params
        self.errlog_path = errlog_path
        self.max_restarts = max_restarts
        self.restarts = 0
        self.session: ClientSession | None = None
        self._specs: list[ToolSpec] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._generation = 0

    async def _start(self) -> None:
        ready, stop = asyncio.Event(), asyncio.Event()
        failure: list[BaseException] = []
        self.session, self._stop = None, stop

        async def owner() -> None:
            try:
                async with AsyncExitStack() as stack:
                    errlog = _server_errlog(self.errlog_path)
                    if errlog is not sys.stderr:
                        stack.callback(errlog.close)
                    read, write = await stack.enter_async_context(stdio_client(self.params, errlog=errlog))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    self.session = session
                    ready.set()
                    await stop.wait()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - reported to the caller below or on the next call
                failure.append(exc)
            finally:
                ready.set()

        self._task = asyncio.create_task(owner())
        await ready.wait()
        if self.session is None:
            raise RuntimeError(f"MCP server failed to start: {failure[0] if failure else 'unknown error'}")

    async def _shutdown(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10)
        self._task, self.session = None, None

    async def __aenter__(self) -> "MCPToolbox":
        await self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._shutdown()

    def _connection_lost(self, exc: BaseException) -> bool:
        if self._task is not None and self._task.done():
            return True
        transport = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream, ConnectionError, EOFError)
        return isinstance(exc, transport) or "connection closed" in str(exc).lower()

    async def list_tools(self) -> list[ToolSpec]:
        assert self.session is not None, "MCPToolbox used outside 'async with'"
        if self._specs is None:
            listed = await self.session.list_tools()
            self._specs = [mcp_tool_to_spec(t) for t in listed.tools]
        return self._specs

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.session is not None, "MCPToolbox used outside 'async with'"
        generation = self._generation
        try:
            return parse_result(await self.session.call_tool(name, args))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - decide below whether this is a dead server
            if not self._connection_lost(exc):
                raise
            async with self._lock:  # many concurrent calls fail together; restart only once
                if self._generation == generation:
                    if self.restarts >= self.max_restarts:
                        raise RuntimeError(f"MCP server keeps dying ({self.restarts} restarts); giving up") from exc
                    with span("mcp.restart", input={"tool": name, "error": f"{type(exc).__name__}: {exc}"}):
                        await self._shutdown()
                        await self._start()
                    self.restarts += 1
                    self._generation += 1
            assert self.session is not None
            return parse_result(await self.session.call_tool(name, args))
