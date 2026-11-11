"""Helpers shared by all agents."""

from __future__ import annotations

import re

from app.models import Inquiry

_TAG = re.compile(r"</?\s*untrusted_email\s*>", re.IGNORECASE)


def render_untrusted(inq: Inquiry) -> str:
    """Wrap customer content as *data*. Any attempt to close the wrapper tag from
    inside the email is neutralised so the content cannot escape the block."""
    body = _TAG.sub("[tag removed]", inq.body)
    subject = _TAG.sub("[tag removed]", inq.subject)
    return (
        "<untrusted_email>\n"
        f"Subject: {subject}\n"
        f"Received: {inq.received_at.date().isoformat()} ({inq.received_at.strftime('%A')})\n"
        f"Body:\n{body}\n"
        "</untrusted_email>"
    )
