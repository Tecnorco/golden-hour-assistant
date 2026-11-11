"""Reads .eml files in, writes approved replies to outbox/ as .eml files."""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from app.adapters.base import Adapter
from app.models import Attachment, Inquiry, OutgoingReply, new_id, utcnow

STUDIO_ADDRESS = "Golden Hour Photography <hello@goldenhourphoto.example>"


class _TextExtractor(HTMLParser):
    """HTML -> text. Keeps *all* visible-or-hidden text (hidden text is exactly
    where injections hide, and guardrails must see it); drops script/style."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class EmlAdapter(Adapter):
    channel = "email"

    def __init__(self, outbox_dir: Path | None = None) -> None:
        self.outbox_dir = outbox_dir

    def parse(self, raw: Path | str | bytes) -> Inquiry:
        source_path: str | None = None
        if isinstance(raw, (Path, str)) and Path(raw).exists():
            source_path = str(raw)
            data = Path(raw).read_bytes()
        elif isinstance(raw, bytes):
            data = raw
        else:
            data = str(raw).encode("utf-8")
        msg = email.message_from_bytes(data, policy=email.policy.default)
        assert isinstance(msg, EmailMessage)

        body_part = msg.get_body(preferencelist=("plain", "html"))
        body = ""
        if body_part is not None:
            content = body_part.get_content()
            body = html_to_text(content) if body_part.get_content_type() == "text/html" else content.strip()

        attachments = [
            Attachment(
                filename=part.get_filename() or "attachment",
                content_type=part.get_content_type(),
                size_bytes=len(part.get_payload(decode=True) or b""),
            )
            for part in msg.iter_attachments()
        ]

        try:
            received = parsedate_to_datetime(msg["Date"]) if msg["Date"] else utcnow()
        except (TypeError, ValueError):
            received = utcnow()
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)

        # Thread on the conversation root so replies stay in one thread.
        refs = (msg.get("References") or "").split()
        thread_id = refs[0] if refs else (msg.get("In-Reply-To") or msg.get("Message-ID") or new_id("thread"))

        return Inquiry(
            source_key=source_key(msg, data),
            channel="email",
            thread_id=str(thread_id).strip(),
            sender=str(msg.get("From", "")),
            subject=str(msg.get("Subject", "")),
            body=body,
            attachments=attachments,
            received_at=received,
            reply_window_expires_at=None,  # email has no reply window
            source_path=source_path,
        )

    def send_reply(self, reply: OutgoingReply) -> str:
        if self.outbox_dir is None:
            raise RuntimeError("EmlAdapter has no outbox_dir configured")
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        msg = EmailMessage()
        msg["From"] = STUDIO_ADDRESS
        msg["To"] = reply.to
        msg["Subject"] = reply.subject
        msg["Date"] = format_datetime(datetime.now(timezone.utc))
        msg["In-Reply-To"] = reply.thread_id
        msg["References"] = reply.thread_id
        msg.set_content(reply.body)
        path = self.outbox_dir / f"{reply.inquiry_id}.eml"
        path.write_bytes(bytes(msg))
        return str(path)


def source_key(msg: EmailMessage, raw: bytes) -> str:
    """Stable identity of an email: its Message-ID (unique per sent email), or a
    hash of the raw file when the header is missing."""
    mid = (msg.get("Message-ID") or "").strip()
    return f"msgid:{mid.lower()}" if mid else f"sha256:{hashlib.sha256(raw).hexdigest()}"


def first_name(sender: str) -> str | None:
    """'Jenna Morales <j@x>' -> 'Jenna'. None when only an address is given."""
    name, _ = parseaddr(sender)
    return name.split()[0] if name.strip() else None
