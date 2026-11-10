from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.observability import tracing

ROOT = Path(__file__).resolve().parents[1]
EMAILS = ROOT / "data" / "emails"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test gets its own var/ dir and an in-memory span sink."""
    monkeypatch.setenv("GHA_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("GHA_OUTBOX_DIR", str(tmp_path / "outbox"))
    get_settings.cache_clear()
    sink = tracing.MemorySink()
    tracing.set_sink(sink)
    yield sink
    get_settings.cache_clear()


@pytest.fixture
def sink(_isolated):
    return _isolated
