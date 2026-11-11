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


def html_text(at: AppTest) -> str:
    return "\n".join(e.proto.body for e in at.get("html"))


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
    store.set_field(blocked.id, "owner_note", "prompt injection suspected")
    return s, store, pending, blocked


def _inbox():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["authenticated"] = True
    return at.run()


def _detail(inquiry_id):
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["authenticated"] = True
    at.session_state["page"] = "detail"
    at.session_state["selected"] = inquiry_id
    at.session_state["detail_pick"] = inquiry_id
    return at.run()


def test_inbox_lists_rows_with_chips_and_icons(seeded):
    _, _, pending, blocked = seeded
    at = _inbox()
    assert not at.exception
    keys = {b.key for b in at.button}
    assert {f"open_{pending.id}", f"open_{blocked.id}"} <= keys
    page = html_text(at)
    assert "Pending approval" in page and "Blocked" in page and "gh-dot" in page
    assert "data:image/svg+xml" in page  # Lucide icons rendered as images (no emoji)
    assert "📷" not in page and "🟡" not in page


def test_clicking_open_navigates_to_the_inquiry(seeded):
    """Regression: Open used to raise StreamlitWidgetAlreadyInstantiatedError."""
    _, _, pending, _ = seeded
    at = _inbox()
    at.button(key=f"open_{pending.id}").click().run()
    assert not at.exception
    assert at.session_state["page"] == "detail" and at.session_state["selected"] == pending.id
    assert "Draft reply" in html_text(at) and at.text_area(key=f"edit_{pending.id}").value.startswith("Hi Jenna")


def test_back_button_returns_to_inbox(seeded):
    _, _, pending, _ = seeded
    at = _detail(pending.id)
    at.button(key="back").click().run()
    assert not at.exception and at.session_state["page"] == "inbox"
    assert "Customer inquiries" in html_text(at)


def test_filter_chips(seeded):
    _, _, pending, blocked = seeded
    at = _inbox()
    at.button_group(key="preset").set_value("Blocked").run()
    keys = {b.key for b in at.button}
    assert f"open_{blocked.id}" in keys and f"open_{pending.id}" not in keys


def test_customer_text_is_escaped_never_rendered_as_html(seeded):
    s, store, *_ = seeded
    evil = EmlAdapter().parse(b"From: Mallory <m@x.example>\nSubject: <img src=x onerror=alert(1)> $5 off\n\n"
                              b"<script>alert('hi')</script> total $35")
    store.add_inquiry(evil, status=S.OWNER_QUEUE.value)
    page = html_text(_inbox()) + html_text(_detail(evil.id))
    assert "<img src=x" not in page and "<script>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "&lt;script&gt;" in page
    assert "&#36;35" in page  # dollar signs escaped so they never become LaTeX


def test_approve_sends_to_outbox_and_ui_never_holds_the_key(seeded):
    s, store, pending, _ = seeded
    at = _detail(pending.id)
    assert not at.exception
    assert "GEMINI_API_KEY" not in os.environ  # removed by the UI on import
    at.button(key="approve").click().run()
    assert store.get_status(pending.id) == "SENT"
    sent = (s.outbox_dir / f"{pending.id}.eml").read_text()
    assert "non-refundable." in sent and "#deposit-to-book#" not in sent
    assert "Approved and sent" in html_text(at)


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
    assert "Needs your attention" in html_text(at)
    at.button(key="unblock").click().run()
    assert store.get_status(blocked.id) == "OWNER_QUEUE"


def test_interrupted_item_can_be_taken_over(seeded):
    s, store, *_ = seeded
    stuck = EmlAdapter().parse(EMAILS / "q02_standard_weekend_addons.eml")
    store.add_inquiry(stuck, status=S.ROUTED.value)  # e.g. the run was stopped mid-way
    at = _detail(stuck.id)
    assert "interrupted" in html_text(at)
    at.button(key="to_owner").click().run()
    assert store.get_status(stuck.id) == "OWNER_QUEUE"
