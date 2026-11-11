"""Typed objects passed between every layer. Pydantic at every boundary."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- channel layer


class Attachment(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class Inquiry(BaseModel):
    """One inbound customer message, normalised from any channel."""

    id: str = Field(default_factory=lambda: new_id("inq"))
    channel: Literal["email", "gmail", "whatsapp"] = "email"
    thread_id: str
    sender: str
    subject: str
    body: str
    attachments: list[Attachment] = Field(default_factory=list)
    received_at: datetime
    # WhatsApp only allows free-form replies for 24h after the customer's last message.
    reply_window_expires_at: datetime | None = None
    source_path: str | None = None  # e.g. the .eml file this came from
    # Identifies the original message so it is never processed twice:
    # "msgid:<message-id>" when the email has a Message-ID header, else "sha256:<file hash>".
    source_key: str | None = None


class OutgoingReply(BaseModel):
    inquiry_id: str
    thread_id: str
    to: str
    subject: str
    body: str


# ---------------------------------------------------------------- routing / extraction

RouteLabel = Literal["quote_request", "question", "reschedule", "other", "spam"]


class RouteDecision(BaseModel):
    label: RouteLabel
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


ServiceType = Literal[
    "mini_portrait", "standard_portrait", "event", "wedding_half_day", "wedding_full_day", "unknown"
]
AddOn = Literal["second_photographer", "extra_photos", "rush_delivery", "printed_album", "engagement_session"]


class ExtractedFields(BaseModel):
    """What the customer told us. `None` means not stated; we never fill gaps by guessing."""

    service_type: ServiceType | None = Field(
        None, description="Best matching package, or 'unknown' if a service is requested but unclear."
    )
    event_date: date | None = Field(None, description="ISO date of the shoot/event, if stated.")
    location: str | None = Field(None, description="Town or venue town, as written by the customer.")
    hours: float | None = Field(None, description="Hours of coverage, only for event coverage.")
    add_ons: list[AddOn] = Field(default_factory=list)
    extra_photo_sets: int = Field(0, description="Number of extra 10-photo sets requested.")
    people_count: int | None = Field(None, description="Guests or people to photograph, if stated.")


REQUIRED_QUOTE_FIELDS = ("service_type", "event_date", "location")


def missing_quote_fields(f: ExtractedFields) -> list[str]:
    """Deterministic: which details we still need before a quote can be calculated."""
    missing: list[str] = []
    if f.service_type in (None, "unknown"):
        missing.append("service_type")
    if f.event_date is None:
        missing.append("event_date")
    if not f.location:
        missing.append("location")
    if f.service_type == "event" and not f.hours:
        missing.append("hours")
    return missing


# ---------------------------------------------------------------- RAG


class Chunk(BaseModel):
    chunk_id: str
    source: str
    heading: str
    text: str


class RetrievedChunk(Chunk):
    score: float


# ---------------------------------------------------------------- tools / quotes


class QuoteLine(BaseModel):
    description: str
    amount: float


class QuoteResult(BaseModel):
    package: str
    package_name: str
    event_date: date
    is_weekend: bool
    distance_miles: float
    lines: list[QuoteLine]
    total: float
    deposit: float
    deposit_pct: int
    notes: list[str] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
    name: str
    args: dict[str, Any]
    result: Any = None
    error: str | None = None


# ---------------------------------------------------------------- drafts / guardrails


class GuardrailVerdict(BaseModel):
    name: str
    passed: bool
    details: str = ""


class Draft(BaseModel):
    inquiry_id: str
    subject: str
    body: str
    sources: list[str] = Field(default_factory=list)  # chunk ids
    tool_results: list[ToolCallRecord] = Field(default_factory=list)
    agent_trace_id: str | None = None
    needs_info: bool = False
    agent: str = ""


class InquiryStatus(str, Enum):
    RECEIVED = "RECEIVED"
    BLOCKED = "BLOCKED"
    ROUTED = "ROUTED"
    DRAFTED = "DRAFTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    EDITED_APPROVED = "EDITED_APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    OWNER_QUEUE = "OWNER_QUEUE"


class Decision(BaseModel):
    inquiry_id: str
    action: Literal["approve", "edit_approve", "reject"]
    edited_body: str | None = None
    decided_at: datetime = Field(default_factory=utcnow)
    note: str = ""


# ---------------------------------------------------------------- observability


class TraceSpan(BaseModel):
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    start: datetime
    end: datetime | None = None
    latency_ms: float | None = None
    input_summary: str = ""
    output_summary: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_args: dict[str, Any] | None = None
    tool_result: Any = None
    verdicts: list[GuardrailVerdict] = Field(default_factory=list)
    error: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
