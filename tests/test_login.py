"""Login gate and demo-data reset for the approval screen."""

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.adapters.eml import EmlAdapter
from app.config import get_settings
from app.models import Draft, InquiryStatus as S
from app.store import Store
from app.ui_kit import auth, demo
from app.ui_kit.theme import esc
from conftest import EMAILS

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "ui" / "streamlit_app.py")
TEST_PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def test_password(monkeypatch):
    # Never put the real demo password in the repo: swap in a known test hash.
    monkeypatch.setattr(auth, "DEFAULT_PASSWORD_HASH", auth.hash_password(TEST_PASSWORD))
    monkeypatch.delenv("GHA_UI_USERNAME", raising=False)
    monkeypatch.delenv("GHA_UI_PASSWORD", raising=False)


@pytest.fixture
def seeded():
    store = Store(get_settings().db_path)
    inq = EmlAdapter().parse(EMAILS / "q01_mini_weekday.eml")
    store.add_inquiry(inq, status=S.PENDING_APPROVAL.value)
    store.add_draft(Draft(inquiry_id=inq.id, subject="Re: x", body="Hi Jenna,\n\nWarmly,\nMaya"))
    return store, inq


def html_text(at):
    return "\n".join(e.proto.body for e in at.get("html"))


def _login(at, user, password):
    at.text_input(key="login_user").input(user)
    at.text_input(key="login_pass").input(password)
    at.button(key="login_submit").click()
    return at.run()


# ---------------------------------------------------------------- unit


def test_plaintext_demo_password_is_not_in_the_repo():
    files = [*(ROOT / "src").rglob("*.py"), ROOT / "ui" / "streamlit_app.py", *(ROOT / "docs").glob("*.md"), ROOT / "README.md"]
    assert auth.DEFAULT_USERNAME == "admin"
    for f in files:
        assert "ILove" not in f.read_text(encoding="utf-8"), f


def test_check_credentials_default_and_override(monkeypatch):
    assert auth.check_credentials("admin", TEST_PASSWORD)
    assert auth.check_credentials(" admin ", TEST_PASSWORD)  # stray spaces in the username are fine
    assert not auth.check_credentials("admin", TEST_PASSWORD + "x")
    assert not auth.check_credentials("root", TEST_PASSWORD)
    monkeypatch.setenv("GHA_UI_USERNAME", "maya")
    monkeypatch.setenv("GHA_UI_PASSWORD", "s3cret-from-secrets")
    assert auth.check_credentials("maya", "s3cret-from-secrets")
    assert not auth.check_credentials("admin", TEST_PASSWORD)  # default stops working once overridden


def test_streamlit_secrets_are_used_when_env_is_not_set():
    secrets = {"GHA_UI_USERNAME": "prof", "GHA_UI_PASSWORD": "from-cloud"}.get
    assert auth.check_credentials("prof", "from-cloud", secrets)

    def missing_secrets_file(name):
        raise FileNotFoundError("no secrets.toml")

    assert auth.check_credentials("admin", TEST_PASSWORD, missing_secrets_file)


def test_lockout_after_repeated_failures():
    state = {}
    for _ in range(auth.MAX_FAILURES - 1):
        assert auth.try_login(state, "admin", "wrong", now=1000.0) == "Incorrect username or password."
    assert "Too many attempts" in auth.try_login(state, "admin", "wrong", now=1000.0)
    assert "Too many attempts" in auth.try_login(state, "admin", TEST_PASSWORD, now=1010.0)  # even the right one
    assert auth.try_login(state, "admin", TEST_PASSWORD, now=1000.0 + auth.LOCKOUT_SECONDS + 1) is None
    assert state["authenticated"] is True


# ---------------------------------------------------------------- UI


def test_nothing_is_shown_before_sign_in(seeded):
    _, inq = seeded
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    page = html_text(at)
    assert "Sign in" in page and "Customer inquiries" not in page
    assert esc(inq.subject) not in page and "Jenna" not in page and not any(b.key and b.key.startswith("open_") for b in at.button)


def test_wrong_password_shows_error_and_stays_locked(seeded):
    at = _login(AppTest.from_file(APP, default_timeout=30).run(), "admin", "nope")
    assert "Incorrect username or password" in html_text(at)
    assert "authenticated" not in at.session_state or not at.session_state["authenticated"]
    assert "Customer inquiries" not in html_text(at)


def test_correct_login_opens_inbox_and_sign_out_returns_to_login(seeded):
    _, inq = seeded
    at = _login(AppTest.from_file(APP, default_timeout=30).run(), "admin", TEST_PASSWORD)
    assert not at.exception
    assert at.session_state["authenticated"] is True
    assert "Customer inquiries" in html_text(at) and esc(inq.subject) in html_text(at)
    at.button(key="nav_logout").click().run()
    assert "Sign in" in html_text(at) and "Customer inquiries" not in html_text(at)


# ---------------------------------------------------------------- demo snapshot


def _snapshot(tmp_path):
    snap = tmp_path / "snapshot.db"
    s = Store(snap)
    inq = EmlAdapter().parse(EMAILS / "f02_faq_delivery.eml")
    s.add_inquiry(inq, status=S.PENDING_APPROVAL.value)
    s.add_draft(Draft(inquiry_id=inq.id, subject="Re: x", body="Hi Chris,\n\nWarmly,\nMaya"))
    return snap, inq


def test_demo_snapshot_seeds_empty_db_but_never_overwrites_real_data(tmp_path):
    snap, inq = _snapshot(tmp_path)
    db = tmp_path / "app" / "assistant.db"
    assert demo.ensure_demo_db(db, snap) is True
    assert Store(db).get_inquiry(inq.id) is not None
    assert demo.ensure_demo_db(db, snap) is False  # already has data: left alone
    assert demo.resolve_snapshot("does/not/exist.db", tmp_path) is None


def test_reset_demo_restores_snapshot_after_approval(tmp_path, monkeypatch):
    snap, inq = _snapshot(tmp_path)
    monkeypatch.setenv("GHA_DEMO_SNAPSHOT", str(snap))
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["authenticated"] = True
    at.session_state["page"], at.session_state["selected"], at.session_state["detail_pick"] = "detail", inq.id, inq.id
    at.run()
    at.button(key="approve").click().run()
    db = get_settings().db_path
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT status FROM inquiries WHERE id=?", (inq.id,)).fetchone()[0] == "SENT"
    at.button(key="nav_reset").click().run()
    assert not at.exception
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT status FROM inquiries WHERE id=?", (inq.id,)).fetchone()[0] == "PENDING_APPROVAL"
    assert not list(get_settings().outbox_dir.glob("*.eml"))
    assert "Demo data reset" in html_text(at)


def test_login_page_shows_the_logo():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert 'src="app/static/logo.png"' in html_text(at)
