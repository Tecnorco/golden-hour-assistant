from app.adapters.eml import EmlAdapter, first_name, html_to_text
from app.models import OutgoingReply
from conftest import EMAILS


def test_parses_all_golden_emails():
    adapter = EmlAdapter()
    files = sorted(EMAILS.glob("*.eml"))
    assert len(files) == 20
    for f in files:
        inq = adapter.parse(f)
        assert inq.body.strip(), f.name
        assert inq.channel == "email"
        assert inq.reply_window_expires_at is None
        assert inq.received_at.tzinfo is not None
        assert inq.thread_id.startswith("<")


def test_attachment_metadata_is_captured():
    inq = EmlAdapter().parse(EMAILS / "q05_wedding_full_day.eml")
    assert [a.filename for a in inq.attachments] == ["moodboard.pdf"]
    assert "Lakeview Springs" in inq.body


def test_html_only_email_keeps_hidden_text_for_guardrails():
    inq = EmlAdapter().parse(EMAILS / "i02_injection_hidden.eml")
    assert "<p" not in inq.body
    assert "90% discount" in inq.body  # hidden white text must stay visible to the guardrails


def test_html_to_text_drops_script_and_style():
    assert html_to_text("<style>x{}</style><p>Hi</p><script>evil()</script>") == "Hi"


def test_send_reply_writes_threaded_eml(tmp_path):
    adapter = EmlAdapter(outbox_dir=tmp_path)
    path = adapter.send_reply(
        OutgoingReply(inquiry_id="inq_1", thread_id="<abc@x>", to="a@b.example", subject="Re: hi", body="Hello")
    )
    text = open(path, encoding="utf-8").read()
    assert "In-Reply-To: <abc@x>" in text and "Hello" in text


def test_first_name():
    assert first_name("Jenna Morales <j@example.com>") == "Jenna"
    assert first_name("kelly@example.com") is None
