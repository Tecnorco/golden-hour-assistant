"""Runs the real CLI commands end to end with no API key: a scripted fake model,
the real MCP server process, real guardrails and SQLite. Guards against a
command handler referencing something that no longer exists."""

import asyncio
from pathlib import Path

import pytest

from app import cli
from app.config import get_settings
from app.llm.client import FunctionCall
from app.llm.fake import FakeLLM
from app.rag.retriever import Retriever, build_index
from app.store import Store
from conftest import EMAILS
from fakes import FakeEmbedder

BODY = """Hi Jenna,

What a lovely surprise for your mom. Here is what that looks like:

- Mini Portrait Session: $175.00
Total: $175.00
Deposit (30%): $52.50

If you'd like to go ahead, just reply and I'll send the deposit invoice.

Warmly,
Maya
Golden Hour Photography"""


@pytest.fixture
def fake_runtime(monkeypatch):
    fake = FakeLLM({
        "injection_classifier": [{"is_injection": False, "confidence": 0.02, "evidence": ""}],
        "router": [{"label": "quote_request", "confidence": 0.97, "reason": "asks for a price"}],
        "extractor": [{"service_type": "mini_portrait", "event_date": "2026-12-09", "location": "Maple Hollow"}],
        "quote_agent": [
            [FunctionCall(name="lookup_distance", args={"town": "Maple Hollow"}),
             FunctionCall(name="check_availability", args={"date": "2026-12-09"})],
            FunctionCall(name="calculate_quote", args={"package": "mini_portrait", "event_date": "2026-12-09", "distance_miles": 8.0}),
            FunctionCall(name="save_draft", args={"subject": "Re: Mini session for my mom's birthday?", "body": BODY}),
        ],
    })
    index = asyncio.run(build_index(get_settings(), FakeEmbedder()))
    monkeypatch.setattr("app.llm.client.GeminiLLM", lambda settings=None: fake)
    monkeypatch.setattr("app.rag.load_retriever", lambda s, llm: Retriever(index, FakeEmbedder(), 3, 0.1))
    return fake


def test_run_then_queue_decide_trace_report(fake_runtime, capsys):
    assert cli.main(["run", str(EMAILS / "q01_mini_weekday.eml")]) == 0
    out = capsys.readouterr().out
    assert "q01_mini_weekday.eml" in out and "PENDING_APPROVAL" in out

    s = get_settings()
    store = Store(s.db_path)
    (row,) = store.list_inquiries()
    iid = row["id"]
    assert row["status"] == "PENDING_APPROVAL"
    assert "$175.00" in store.latest_draft(iid).body  # saved by the MCP server process

    assert cli.main(["queue", "--status", "PENDING_APPROVAL"]) == 0
    assert iid in capsys.readouterr().out

    assert cli.main(["trace", iid]) == 0
    trace_out = capsys.readouterr().out
    assert "pipeline" in trace_out and "tool.calculate_quote" in trace_out

    assert cli.main(["decide", iid, "approve", "--note", "ok"]) == 0
    assert "Approved and written to" in capsys.readouterr().out
    assert (s.outbox_dir / f"{iid}.eml").exists() and store.get_status(iid) == "SENT"

    assert cli.main(["report"]) == 0
    assert "Approved as drafted: 1" in capsys.readouterr().out


def test_offline_commands_run(capsys):
    assert cli.main(["parse", str(EMAILS / "s01_spam.eml")]) == 0
    assert "SEO" in capsys.readouterr().out
    assert cli.main(["eval", "--compare"]) == 1  # no runs yet: clear message, no crash
    assert "Need two eval runs" in capsys.readouterr().out
    assert cli.main(["doctor", "--offline"]) == 1  # no key / no index in the test env
    assert "fix:" in capsys.readouterr().out


def _undefined_names(path: Path) -> list[str]:
    """Names a function reads that are bound nowhere: not in the function (or an
    enclosing one), not at module level, not a builtin. A small, dependency-free
    stand-in for pyflakes' undefined-name check."""
    import ast
    import builtins

    tree = ast.parse(path.read_text(encoding="utf-8"))

    def bound(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.arg):
                names.add(n.arg)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                names |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
        return names

    module_names = bound(tree) | set(dir(builtins)) | {"__file__", "__name__"}
    problems = []
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local = bound(top)
            for n in ast.walk(top):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in local | module_names:
                    problems.append(f"{path.name}:{n.lineno} {n.id}")
    return problems


def test_no_undefined_names_anywhere():
    root = Path(__file__).resolve().parents[1]
    files = [*(root / "src").rglob("*.py"), root / "ui" / "streamlit_app.py"]
    problems = [p for f in files for p in _undefined_names(f)]
    assert problems == []


def test_undefined_name_checker_catches_the_original_bug(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def cmd_run(args):\n    return build_pipeline_and_run(args)\n")
    assert _undefined_names(bad) == ["bad.py:2 build_pipeline_and_run"]
