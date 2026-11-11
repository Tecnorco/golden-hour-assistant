"""Drives the Streamlit approval screen headlessly with streamlit's AppTest."""

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.adapters.eml import EmlAdapter
from app.config import get_settings
from app.models import Draft, InquiryStatus as S
from app.store import Store
from conftest import EMAILS

APP = str(Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py")


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.ui-must-not-see-this")
    s = get_settings()
    store = Store(s.db_path)
    adapter = EmlAdapter()
    pending = adapter.parse(EMAILS / "q01_mini_weekday.eml")
    store.add_inquiry(pending, status=S.PENDING_APPROVAL.value)
    store.add_draft(Draft(inquiry_id=pending.id, subject="Re: Mini session",
                          body="Hi Jenna,\n\nThe deposit is non-refundable. [deposits-and-payment#deposit-to-book#0]\n\nWarmly,\nMaya"))
    blocked = adapter.parse(EMAILS / "i01_injection_blatant.eml")
    store.add_inquiry(blocked, status=S.BLOCKED.value)
    return s, store, pending, blocked


def _detail(inquiry_id):
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["page"] = "Inquiry detail"
    at.session_state["selected"] = inquiry_id
    return at.run()


def test_queue_page_lists_actionable_items(seeded):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert len(at.dataframe) == 1
    assert "Rejected" not in at.markdown[0].value


def test_approve_sends_to_outbox_and_ui_never_holds_the_key(seeded):
    s, store, pending, _ = seeded
    at = _detail(pending.id)
    assert not at.exception
    assert "GEMINI_API_KEY" not in os.environ  # removed by the UI on import
    at.button(key="approve").click().run()
    assert store.get_status(pending.id) == "SENT"
    sent = (s.outbox_dir / f"{pending.id}.eml").read_text()
    assert "non-refundable." in sent and "#deposit-to-book#" not in sent
    assert any("sent" in m.value for m in at.success)


def test_approve_with_unsaved_edits_is_refused(seeded):
    s, store, pending, _ = seeded
    at = _detail(pending.id)
    at.text_area(key=f"edit_{pending.id}").set_value("Hi Jenna, totally different").run()
    at.button(key="approve").click().run()
    assert store.get_status(pending.id) == "PENDING_APPROVAL"
    assert any("Edit, approve & send" in e.value for e in at.error)


def test_edit_and_approve_sends_edited_text(seeded):
    s, store, pending, _ = seeded
    at = _detail(pending.id)
    at.text_area(key=f"edit_{pending.id}").set_value("Hi Jenna,\n\nSee you on the 9th.\n\nWarmly,\nMaya").run()
    at.button(key="edit_approve").click().run()
    assert store.get_status(pending.id) == "SENT"
    assert "See you on the 9th" in (s.outbox_dir / f"{pending.id}.eml").read_text()


def test_reject_sends_nothing(seeded):
    s, store, pending, _ = seeded
    at = _detail(pending.id)
    at.button(key="reject").click().run()
    assert store.get_status(pending.id) == "REJECTED"
    assert not (s.outbox_dir / f"{pending.id}.eml").exists()


def test_blocked_item_cannot_be_approved_until_reviewed(seeded):
    s, store, _, blocked = seeded
    at = _detail(blocked.id)
    assert at.button(key="approve").disabled and at.button(key="edit_approve").disabled
    at.button(key="unblock").click().run()
    assert store.get_status(blocked.id) == "OWNER_QUEUE"
