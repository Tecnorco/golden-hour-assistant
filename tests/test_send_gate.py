"""The non-negotiable rule: nothing is sent without recorded owner approval."""

from pathlib import Path

import pytest

from app.adapters.eml import EmlAdapter
from app.models import Decision, Draft, InquiryStatus as S
from app.orchestrator import InvalidTransition, SendBlocked, record_decision, send_reply
from app.store import Store
from conftest import EMAILS

CID = "deposits-and-payment#deposit-to-book#0"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "g.db")
    outbox = tmp_path / "outbox"
    adapter = EmlAdapter(outbox_dir=outbox)
    inq = adapter.parse(EMAILS / "q01_mini_weekday.eml")
    store.add_inquiry(inq, status=S.PENDING_APPROVAL.value)
    store.add_draft(Draft(inquiry_id=inq.id, subject="Re: Mini session", body=f"Hi Jenna, the deposit is non-refundable. [{CID}]"))
    return store, adapter, outbox, inq


@pytest.mark.parametrize("status", [s for s in S if s not in (S.APPROVED, S.EDITED_APPROVED)])
def test_send_raises_for_every_unapproved_status(env, status):
    store, adapter, outbox, inq = env
    store.set_status(inq.id, None, status.value)
    with pytest.raises(SendBlocked):
        send_reply(store, adapter, inq.id)
    assert not outbox.exists() or not any(outbox.iterdir())  # nothing written


def test_status_forged_without_decision_is_still_blocked(env):
    store, adapter, outbox, inq = env
    store.set_status(inq.id, None, S.APPROVED.value)  # e.g. someone edits the DB directly
    with pytest.raises(SendBlocked, match="no recorded owner approval"):
        send_reply(store, adapter, inq.id)


def test_approved_reply_is_sent_once_without_citation_markers(env):
    store, adapter, outbox, inq = env
    record_decision(store, Decision(inquiry_id=inq.id, action="approve"))
    path = send_reply(store, adapter, inq.id)
    text = Path(path).read_text()
    assert "non-refundable." in text and CID not in text and "In-Reply-To: <q01_mini_weekday@mail.example>" in text
    assert store.get_status(inq.id) == "SENT"
    with pytest.raises(SendBlocked):  # no double send
        send_reply(store, adapter, inq.id)
    assert len(list(outbox.iterdir())) == 1


def test_edit_and_approve_sends_the_edited_text_and_keeps_history(env):
    store, adapter, outbox, inq = env
    record_decision(store, Decision(inquiry_id=inq.id, action="edit_approve", edited_body="Hi Jenna, see you soon!", note="shorter"))
    assert store.get_status(inq.id) == "EDITED_APPROVED"
    text = Path(send_reply(store, adapter, inq.id)).read_text()
    assert "see you soon" in text
    assert store.latest_draft(inq.id).agent == "owner"
    d = store.decisions(inq.id)
    assert len(d) == 1 and d[0]["decided_at"]


def test_rejected_is_never_sent_and_cannot_be_approved_after(env):
    store, adapter, outbox, inq = env
    record_decision(store, Decision(inquiry_id=inq.id, action="reject", note="I'll call them"))
    with pytest.raises(SendBlocked):
        send_reply(store, adapter, inq.id)
    with pytest.raises(InvalidTransition):
        record_decision(store, Decision(inquiry_id=inq.id, action="approve"))


def test_edit_approve_requires_text(env):
    store, *_ , inq = env
    with pytest.raises(ValueError):
        record_decision(store, Decision(inquiry_id=inq.id, action="edit_approve", edited_body="  "))


def test_owner_queue_items_can_be_approved_by_the_owner(env):
    store, adapter, outbox, inq = env
    store.set_status(inq.id, None, S.OWNER_QUEUE.value)
    record_decision(store, Decision(inquiry_id=inq.id, action="edit_approve", edited_body="Hi Jenna, fixed by hand."))
    assert Path(send_reply(store, adapter, inq.id)).exists()


def test_adapter_is_only_called_through_the_gate():
    # Static check: send_reply is the only production caller of adapter.send_reply.
    root = Path(__file__).resolve().parents[1] / "src"
    callers = [p for p in root.rglob("*.py") if ".send_reply(" in p.read_text() and p.name != "base.py"]
    assert [p.name for p in callers] == ["orchestrator.py"]
