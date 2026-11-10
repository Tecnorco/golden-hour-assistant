import asyncio

import pytest
from google.genai import errors as genai_errors
from pydantic import SecretStr

from app.config import get_settings
from app.llm.client import GeminiLLM, LLMError, ToolSpec, estimate_cost, parse_structured, to_function_declarations
from app.llm.fake import FakeLLM
from app.models import ExtractedFields, RouteDecision


def _llm(attempts: int = 3) -> GeminiLLM:
    s = get_settings().model_copy(update={"api_key": SecretStr("dummy"), "llm_retry_attempts": attempts})
    return GeminiLLM(s)  # constructing the client makes no network call


def test_parse_structured_accepts_fenced_json():
    out = parse_structured('```json\n{"label":"spam","confidence":0.9,"reason":"ad"}\n```', RouteDecision)
    assert out.label == "spam"


def test_parse_structured_rejects_bad_label():
    with pytest.raises(LLMError):
        parse_structured('{"label":"buy_now","confidence":0.9,"reason":"x"}', RouteDecision)


def test_retry_on_transient_then_success(monkeypatch):
    llm = _llm()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})
        return "ok"

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    assert asyncio.run(llm._with_retry(flaky, "t")) == "ok"
    assert calls["n"] == 3


def test_no_retry_on_client_error(monkeypatch):
    llm = _llm()
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise genai_errors.ClientError(400, {"error": {"message": "bad request"}})

    with pytest.raises(LLMError):
        asyncio.run(llm._with_retry(bad, "t"))
    assert calls["n"] == 1


def test_retry_gives_up_after_attempts(monkeypatch):
    llm = _llm(attempts=2)

    async def timeout():
        raise TimeoutError("slow")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(LLMError, match="2 attempt"):
        asyncio.run(llm._with_retry(timeout, "t"))


def test_missing_key_is_a_clear_error():
    s = get_settings().model_copy(update={"api_key": None})
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM(s)


def test_settings_never_expose_key():
    s = get_settings().model_copy(update={"api_key": SecretStr("AQ.supersecretvalue")})
    assert "supersecret" not in repr(s) and "supersecret" not in s.model_dump_json()


def test_cost_estimate_uses_price_table():
    s = get_settings()
    assert estimate_cost(s, "gemini-3.8-flash", 1_000_000, 0) == pytest.approx(s.prices["gemini-3.8-flash"].input_per_m)
    assert estimate_cost(s, "unknown-model", 1000, 1000) == 0.0


def test_mcp_style_schema_becomes_function_declaration():
    tools = to_function_declarations(
        [ToolSpec(name="check_availability", description="d", parameters={"type": "object", "properties": {"date": {"type": "string"}}})]
    )
    decl = tools[0].function_declarations[0]
    assert decl.name == "check_availability" and decl.parameters_json_schema["properties"]["date"]["type"] == "string"


def test_fake_llm_structured_and_traced(sink):
    fake = FakeLLM({"extractor": [{"service_type": "mini_portrait", "location": "Riverbend"}]})
    resp = asyncio.run(fake.generate(prompt_name="extractor", system="s", turns="t", schema=ExtractedFields))
    assert resp.parsed.location == "Riverbend"
    assert any(s.name == "llm.extractor" and s.tokens_in == 10 for s in sink.spans)
