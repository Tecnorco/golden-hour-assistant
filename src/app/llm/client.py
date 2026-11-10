"""Thin wrapper over google-genai.

Responsibilities (and nothing more):
- timeouts, plus retry with exponential backoff on transient errors;
- structured output: a pydantic model in, a validated instance out;
- function calling, while keeping the agent loop in *our* code
  (automatic function calling is disabled);
- one trace span per call with model, latency, tokens and estimated cost.

Agents talk to the `LLM` protocol, so tests swap in `FakeLLM` and need no key.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Literal, Protocol, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings, get_settings
from app.observability.tracing import record_llm_usage, span

T = TypeVar("T", bound=BaseModel)

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- our message types


class FunctionCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class FunctionResult(BaseModel):
    name: str
    response: dict[str, Any]
    id: str | None = None


class Turn(BaseModel):
    """One conversation turn. `raw` keeps the SDK Content for model turns so
    Gemini 3 thought signatures are echoed back unchanged on the next request."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: Literal["user", "model", "tool"]
    text: str | None = None
    function_calls: list[FunctionCall] = Field(default_factory=list)
    function_results: list[FunctionResult] = Field(default_factory=list)
    raw: Any = Field(default=None, exclude=True)


class ToolSpec(BaseModel):
    """A callable tool described by JSON Schema (we get these from MCP)."""

    name: str
    description: str
    parameters: dict[str, Any]


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class LLMResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str = ""
    parsed: Any = None
    function_calls: list[FunctionCall] = Field(default_factory=list)
    turn: Turn
    usage: Usage = Field(default_factory=Usage)
    model: str = ""


class LLM(Protocol):
    async def generate(
        self,
        *,
        prompt_name: str,
        system: str,
        turns: list[Turn] | str,
        schema: type[BaseModel] | None = None,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse: ...

    async def embed(self, texts: list[str], task_type: str) -> list[list[float]]: ...


# ---------------------------------------------------------------- conversion helpers


def _to_content(turn: Turn) -> types.Content:
    if turn.raw is not None:
        return turn.raw
    parts: list[types.Part] = []
    if turn.text:
        parts.append(types.Part(text=turn.text))
    for fc in turn.function_calls:
        parts.append(types.Part(function_call=types.FunctionCall(name=fc.name, args=fc.args, id=fc.id)))
    for fr in turn.function_results:
        parts.append(
            types.Part(function_response=types.FunctionResponse(name=fr.name, response=fr.response, id=fr.id))
        )
    # Function results go back to Gemini with role "user".
    role = "model" if turn.role == "model" else "user"
    return types.Content(role=role, parts=parts)


def to_function_declarations(tools: list[ToolSpec]) -> list[types.Tool]:
    """MCP tool schemas are JSON Schema, which Gemini accepts directly."""
    decls = [
        types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
        for t in tools
    ]
    return [types.Tool(function_declarations=decls)]


def estimate_cost(settings: Settings, model: str, tokens_in: int, tokens_out: int) -> float:
    p = settings.price_for(model)
    return round(tokens_in / 1e6 * p.input_per_m + tokens_out / 1e6 * p.output_per_m, 6)


_TRANSIENT_NAMES = ("Timeout", "ConnectError", "TransportError", "RemoteProtocolError", "ReadError")


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        return exc.code in TRANSIENT_STATUS
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    # The SDK's HTTP transport has changed between releases (httpx -> httpx2), so
    # match transport errors by class name instead of importing a transitive dependency.
    return any(any(n in cls.__name__ for n in _TRANSIENT_NAMES) for cls in type(exc).__mro__)


# ---------------------------------------------------------------- Gemini implementation


class GeminiLLM:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        if self.s.api_key is None:
            raise LLMError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        self._client: genai.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def client(self) -> genai.Client:
        """One SDK client per event loop. The SDK's async HTTP session is bound to
        the loop that first used it, so reusing it from a new `asyncio.run()`
        (CLI commands, Streamlit button clicks) fails with "Event loop is closed"."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            assert self.s.api_key is not None
            self._client = genai.Client(
                api_key=self.s.api_key.get_secret_value(),
                http_options=types.HttpOptions(
                    timeout=int(self.s.llm_timeout_s * 1000),
                    # We retry ourselves (see _with_retry) so the policy is visible and traced.
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            self._loop = loop
        return self._client

    async def _with_retry(self, fn: Any, what: str) -> Any:
        attempts = max(1, self.s.llm_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await fn()
            except Exception as exc:  # noqa: BLE001 - filtered by _is_transient and re-raised
                if not _is_transient(exc) or attempt == attempts:
                    raise LLMError(f"{what} failed after {attempt} attempt(s): {type(exc).__name__}: {exc}") from exc
                delay = min(30.0, 1.0 * 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

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
        contents = [_to_content(t) for t in turns]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = schema.model_json_schema()
        if tools:
            config.tools = to_function_declarations(tools)

        model = self.s.chat_model
        with span(f"llm.{prompt_name}", input={"model": model, "turns": len(contents)}) as sp:
            t0 = time.perf_counter()
            resp = await self._with_retry(
                lambda: self.client.aio.models.generate_content(model=model, contents=contents, config=config),
                f"generate({prompt_name})",
            )
            latency = (time.perf_counter() - t0) * 1000
            um = resp.usage_metadata
            tin = (um.prompt_token_count or 0) if um else 0
            # Thinking tokens are billed as output.
            tout = ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0
            cost = estimate_cost(self.s, model, tin, tout)
            record_llm_usage(tin, tout, cost, model=model, prompt=prompt_name, latency_ms=round(latency, 1))

            cand = resp.candidates[0] if resp.candidates else None
            content = cand.content if cand and cand.content else types.Content(role="model", parts=[])
            calls = [
                FunctionCall(name=p.function_call.name or "", args=dict(p.function_call.args or {}), id=p.function_call.id)
                for p in (content.parts or [])
                if p.function_call
            ]
            text = "".join(p.text for p in (content.parts or []) if p.text and not p.thought)
            out = LLMResponse(
                text=text,
                function_calls=calls,
                turn=Turn(role="model", text=text or None, function_calls=calls, raw=content),
                usage=Usage(tokens_in=tin, tokens_out=tout, cost_usd=cost, latency_ms=round(latency, 1)),
                model=model,
            )
            if schema is not None:
                out.parsed = parse_structured(text, schema)
            sp.output = calls or text
            return out

    async def embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        model = self.s.embedding_model
        with span("llm.embed", input={"model": model, "n": len(texts), "task_type": task_type}):
            resp = await self._with_retry(
                lambda: self.client.aio.models.embed_content(
                    model=model,
                    contents=texts,
                    config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=self.s.embedding_dim),
                ),
                "embed",
            )
            # The embed API returns no usage metadata; estimate ~1.3 tokens per word.
            tin = int(sum(len(t.split()) for t in texts) * 1.3)
            record_llm_usage(tin, 0, estimate_cost(self.s, model, tin, 0), model=model, prompt="embed")
            return [list(e.values or []) for e in (resp.embeddings or [])]


def parse_structured(text: str, schema: type[T]) -> T:
    """Validate model JSON against the pydantic schema; tolerate ```json fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned
    try:
        return schema.model_validate_json(cleaned)
    except ValidationError as exc:
        raise LLMError(f"model output did not match {schema.__name__}: {exc.errors()[:3]}") from exc
