"""Skipping already-processed emails (run / watch / demo-snapshot)."""

import shutil
import sqlite3
import time
from pathlib import Path

from app import cli
from app.adapters.eml import EmlAdapter
from app.config import get_settings
from app.intake import backfill_source_keys, scan, select_new
from app.models import InquiryStatus as S
from app.store import Store
from conftest import EMAILS
from test_cli import fake_runtime  # noqa: F401 - pytest fixture: scripted model, real MCP server

ADAPTER = EmlAdapter()
Q01 = EMAILS / "q01_mini_weekday.eml"


def test_source_key_uses_message_id_or_file_hash(tmp_path):
    assert ADAPTER.parse(Q01).source_key == "msgid:<q01_mini_weekday@mail.example>"
    raw = b"From: a@b.example\nSubject: hi\n\nno message id here"
    k1, k2 = ADAPTER.parse(raw).source_key, ADAPTER.parse(raw).source_key
    assert k1.startswith("sha256:") and k1 == k2
    assert ADAPTER.parse(raw + b"!").source_key != k1


def test_select_new_skips_processed_batch_duplicates_and_respects_force(tmp_path):
    store = Store(tmp_path / "a.db")
    copy = tmp_path / "copy_of_q01.eml"
    shutil.copy(Q01, copy)
    other = EMAILS / "q02_standard_weekend_addons.eml"

    first = select_new([Q01, copy, other], store, ADAPTER)
    assert [p.name for p, _ in first.new] == ["q01_mini_weekday.eml", "q02_standard_weekend_addons.eml"]
    assert first.skipped == [(copy, "same email as q01_mini_weekday.eml")]

    store.add_inquiry(first.new[0][1], status=S.PENDING_APPROVAL.value)
    second = select_new([Q01, other], store, ADAPTER)
    assert [p.name for p, _ in second.new] == ["q02_standard_weekend_addons.eml"]
    assert "already processed as inq_" in second.skipped[0][1]

    assert len(select_new([Q01, other], store, ADAPTER, force=True).new) == 2


def test_files_still_being_copied_wait_for_the_next_check(tmp_path):
    fresh = tmp_path / "new.eml"
    shutil.copy(Q01, fresh)
    sel = select_new([fresh], Store(tmp_path / "a.db"), ADAPTER, min_age_seconds=5, now=time.time())
    assert sel.waiting == [fresh] and not sel.new
    later = select_new([fresh], Store(tmp_path / "a.db"), ADAPTER, min_age_seconds=5, now=time.time() + 10)
    assert len(later.new) == 1


def test_unreadable_file_is_reported_not_fatal(tmp_path):
    bad = tmp_path / "broken.eml"
    bad.mkdir()  # a folder named .eml cannot be read as a file
    sel = select_new([bad, Q01], Store(tmp_path / "a.db"), ADAPTER)
    assert sel.unreadable and sel.unreadable[0][0] == bad and len(sel.new) == 1
    assert scan(tmp_path) == [bad]


def test_old_database_gets_the_column_and_keys_are_backfilled(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:  # schema from before source keys existed
        c.execute("CREATE TABLE inquiries (id TEXT PRIMARY KEY, status TEXT NOT NULL, received_at TEXT NOT NULL, "
                  "data TEXT NOT NULL, route TEXT, extracted TEXT, verdicts TEXT, owner_note TEXT, updated_at TEXT NOT NULL)")
    store = Store(db)
    with_file = ADAPTER.parse(Q01).model_copy(update={"source_key": None})
    moved = ADAPTER.parse(EMAILS / "f02_faq_delivery.eml").model_copy(
        update={"source_key": None, "source_path": "C:/somewhere/else/f02.eml"})  # e.g. a snapshot from another computer
    store.add_inquiry(with_file)
    store.add_inquiry(moved)
    assert backfill_source_keys(store, ADAPTER) == 2
    assert store.find_by_source_key("msgid:<q01_mini_weekday@mail.example>") == with_file.id
    assert store.find_by_source_key("msgid:<f02_faq_delivery@mail.example>") == moved.id
    assert backfill_source_keys(store, ADAPTER) == 0


def test_run_twice_second_time_costs_nothing(fake_runtime, capsys):  # noqa: F811
    assert cli.main(["run", str(Q01)]) == 0
    calls_after_first = len(fake_runtime.calls)
    assert calls_after_first > 0
    capsys.readouterr()
    assert cli.main(["run", str(Q01)]) == 0
    out = capsys.readouterr().out
    assert "skipped: already processed as inq_" in out and "No new emails to process." in out
    assert len(fake_runtime.calls) == calls_after_first  # not a single model call
    assert len(Store(get_settings().db_path).list_inquiries()) == 1


def test_run_force_reprocesses(fake_runtime, capsys):  # noqa: F811
    cli.main(["run", str(Q01)])
    cli.main(["run", "--force", str(Q01)])
    assert len(Store(get_settings().db_path).list_inquiries()) == 2


def test_watch_once_picks_up_only_new_files(fake_runtime, tmp_path, capsys):  # noqa: F811
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(Q01, inbox / "a.eml")
    assert cli.main(["watch", "--folder", str(inbox), "--once", "--settle", "0"]) == 0
    assert "processed 1 new email" in capsys.readouterr().out
    calls = len(fake_runtime.calls)
    assert cli.main(["watch", "--folder", str(inbox), "--once", "--settle", "0"]) == 0
    assert "No new emails to process." in capsys.readouterr().out and len(fake_runtime.calls) == calls


def test_demo_snapshot_reuses_previous_snapshot(fake_runtime, tmp_path, monkeypatch, capsys):  # noqa: F811
    one = tmp_path / "emails"
    one.mkdir()
    shutil.copy(Q01, one / "q01.eml")
    monkeypatch.setenv("GHA_DATA_DIR", str(tmp_path / "data"))
    shutil.copytree(EMAILS.parent, tmp_path / "data", dirs_exist_ok=True)
    shutil.rmtree(tmp_path / "data" / "emails")
    shutil.copytree(one, tmp_path / "data" / "emails")
    monkeypatch.setenv("GHA_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.setenv("GHA_OUTBOX_DIR", str(tmp_path / "unused-outbox"))
    get_settings.cache_clear()
    out_db = tmp_path / "snap" / "assistant.db"
    assert cli.main(["demo-snapshot", "--output", str(out_db)]) == 0
    calls = len(fake_runtime.calls)
    get_settings.cache_clear()
    assert cli.main(["demo-snapshot", "--output", str(out_db)]) == 0
    assert len(fake_runtime.calls) == calls  # second build reused the snapshot: no model calls
    with sqlite3.connect(out_db) as c:
        assert c.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0] == 1
