"""Channel adapter interface.

An adapter is the only code that knows about a channel's wire format. It turns
an inbound message into an `Inquiry` and delivers an approved `OutgoingReply`.
Agents never import adapters, so Gmail or WhatsApp can be added without
touching them.

Future adapters (not built, by design):
- GmailAdapter: parse() from the Gmail API message resource; send_reply() replies
  in-thread using thread_id and In-Reply-To headers.
- WhatsAppAdapter: parse() from a webhook payload; set reply_window_expires_at to
  last customer message + 24h, keep per-phone-number memory keyed by thread_id,
  and refuse send_reply() after the window closes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models import Inquiry, OutgoingReply


class Adapter(ABC):
    channel: str

    @abstractmethod
    def parse(self, raw: Any) -> Inquiry:
        """Turn one raw inbound message into an Inquiry."""

    @abstractmethod
    def send_reply(self, reply: OutgoingReply) -> str:
        """Deliver an approved reply. Returns a delivery reference (path, message id...).

        Must only be called through the orchestrator's send gate.
        """
