"""All settings in one place. Every value can be overridden with an env var.

Secrets (GEMINI_API_KEY) are read here but never stored on the Settings object's
repr or logged: `api_key` is a SecretStr.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr

ROOT = Path(__file__).resolve().parents[2]


class ModelPrice(BaseModel):
    """USD per 1M tokens. Used only for cost *estimates* in traces and reports."""

    input_per_m: float
    output_per_m: float


# Verify against the current Gemini pricing page before quoting costs to anyone.
# Override with GHA_PRICE_<MODEL>_IN / _OUT (model name upper-cased, '-' and '.' -> '_').
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "gemini-3.8-flash": ModelPrice(input_per_m=0.30, output_per_m=2.50),
    "gemini-embedding-001": ModelPrice(input_per_m=0.15, output_per_m=0.0),
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Settings(BaseModel):
    api_key: SecretStr | None
    chat_model: str
    embedding_model: str
    embedding_dim: int

    # LLM call behaviour
    llm_timeout_s: float
    llm_retry_attempts: int

    # Orchestration thresholds
    router_confidence_threshold: float
    agent_max_steps: int
    retrieval_top_k: int
    retrieval_min_score: float
    chunk_target_tokens: int
    chunk_min_tokens: int
    chunk_overlap_tokens: int
    max_input_chars: int

    # Paths
    data_dir: Path
    prompts_dir: Path
    db_path: Path
    index_dir: Path
    outbox_dir: Path
    reports_dir: Path

    prices: dict[str, ModelPrice]

    def price_for(self, model: str) -> ModelPrice:
        return self.prices.get(model, ModelPrice(input_per_m=0.0, output_per_m=0.0))


def _prices() -> dict[str, ModelPrice]:
    prices = dict(DEFAULT_PRICES)
    for model, p in DEFAULT_PRICES.items():
        key = model.upper().replace("-", "_").replace(".", "_")
        prices[model] = ModelPrice(
            input_per_m=float(_env(f"GHA_PRICE_{key}_IN", str(p.input_per_m))),
            output_per_m=float(_env(f"GHA_PRICE_{key}_OUT", str(p.output_per_m))),
        )
    return prices


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY") or None
    var = Path(_env("GHA_VAR_DIR", str(ROOT / "var")))
    return Settings(
        api_key=SecretStr(key) if key else None,
        chat_model=_env("GEMINI_CHAT_MODEL", "gemini-3.8-flash"),
        embedding_model=_env("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        embedding_dim=int(_env("GHA_EMBEDDING_DIM", "768")),
        llm_timeout_s=float(_env("GHA_LLM_TIMEOUT_S", "60")),
        llm_retry_attempts=int(_env("GHA_LLM_RETRY_ATTEMPTS", "4")),
        router_confidence_threshold=float(_env("GHA_ROUTER_CONFIDENCE_THRESHOLD", "0.7")),
        agent_max_steps=int(_env("GHA_AGENT_MAX_STEPS", "8")),
        retrieval_top_k=int(_env("GHA_RETRIEVAL_TOP_K", "4")),
        retrieval_min_score=float(_env("GHA_RETRIEVAL_MIN_SCORE", "0.62")),
        chunk_target_tokens=int(_env("GHA_CHUNK_TARGET_TOKENS", "300")),
        chunk_min_tokens=int(_env("GHA_CHUNK_MIN_TOKENS", "40")),
        chunk_overlap_tokens=int(_env("GHA_CHUNK_OVERLAP_TOKENS", "50")),
        max_input_chars=int(_env("GHA_MAX_INPUT_CHARS", "8000")),
        data_dir=Path(_env("GHA_DATA_DIR", str(ROOT / "data"))),
        prompts_dir=Path(_env("GHA_PROMPTS_DIR", str(ROOT / "prompts"))),
        db_path=Path(_env("GHA_DB_PATH", str(var / "assistant.db"))),
        index_dir=Path(_env("GHA_INDEX_DIR", str(var / "index"))),
        outbox_dir=Path(_env("GHA_OUTBOX_DIR", str(ROOT / "outbox"))),
        reports_dir=Path(_env("GHA_REPORTS_DIR", str(ROOT / "evals" / "reports"))),
        prices=_prices(),
    )
