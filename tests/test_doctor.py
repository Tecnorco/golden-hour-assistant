import asyncio

from app.config import get_settings
from app.doctor import print_checks, run_checks


def test_doctor_offline_reports_missing_key_and_index_with_fixes(capsys):
    checks = {c.name: c for c in asyncio.run(run_checks(get_settings(), offline=True))}
    assert checks["python"].status == "PASS"
    assert checks["demo data"].status == "PASS" and checks["prompts"].status == "PASS"
    assert checks["sqlite"].status == "PASS"
    assert checks["mcp server"].status == "PASS"  # spawns the real server
    assert checks["api key"].status == "FAIL" and ".env" in checks["api key"].fix
    assert checks["vector index"].status == "FAIL" and "make index" in checks["vector index"].fix
    assert {checks[n].status for n in ("gemini chat", "gemini embeddings", "end to end")} == {"SKIP"}
    assert print_checks(list(checks.values())) == 1
    out = capsys.readouterr().out
    assert "fix: Build it: `make index`." in out and "2 failed" in out
