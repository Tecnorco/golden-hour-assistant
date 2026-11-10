from app.guardrails.redact import redact, summarize


def test_redacts_email_phone_card_address_and_keys():
    text = (
        "Mail jenna.morales@example.com or call (555) 214-8890 / 555.301.7765. "
        "Card 4111 1111 1111 1111. I live at 12 Oak Street. key AIzaSyA1234567890abcdefghijk"
    )
    out = redact(text)
    for leaked in ["jenna.morales", "214-8890", "301.7765", "4111", "12 Oak Street", "AIzaSy"]:
        assert leaked not in out
    assert "[EMAIL]" in out and "[PHONE]" in out and "[CARD]" in out and "[ADDRESS]" in out and "[SECRET]" in out


def test_keeps_prices_and_dates():
    out = redact("Total $1,142.50 on 2027-02-14 for 3 hours")
    assert out == "Total $1,142.50 on 2027-02-14 for 3 hours"


def test_summarize_caps_length():
    assert len(summarize("x" * 1000, limit=50)) == 50
