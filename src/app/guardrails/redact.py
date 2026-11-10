"""PII redaction for anything written to logs or traces.

Deliberately conservative regexes: we would rather over-redact a log line than
leak a customer's phone number into a trace table.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # Card-like digit runs first so they are not half-eaten by the phone pattern.
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"(?<!\w)(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"), "[PHONE]"),
    (re.compile(r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Ln|Lane|Dr|Drive|Blvd|Ct|Way)\b\.?"), "[ADDRESS]"),
    # API-key shaped secrets, in case one ever ends up in an error message.
    (re.compile(r"\b(?:AIza[0-9A-Za-z_-]{20,}|AQ\.[0-9A-Za-z_-]{20,})"), "[SECRET]"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def summarize(value: object, limit: int = 300) -> str:
    """Redacted, length-capped one-line summary for trace input/output fields."""
    text = value if isinstance(value, str) else repr(value)
    text = redact(" ".join(text.split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"
