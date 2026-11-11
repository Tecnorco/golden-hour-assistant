"""Output guardrails: deterministic checks on every draft before the owner sees it.

1. amounts_match_tools    every $ amount equals a number from the tool results
                          (or, for policy facts, appears in a retrieved chunk).
2. citations_valid        every cited chunk id was actually retrieved.
3. policy_claims_cited    sentences stating concrete policy facts carry a citation.
4. no_unconfirmed_promises no discounts, no booking confirmations, no "available"
                          for a date the calendar says is taken, no dates that
                          neither the customer nor a tool mentioned.
5. tone_and_length        brand-voice rules: greeting, sign-off, length,
                          exclamation marks, banned phrases, no emojis.
6. no_internal_leakage    no tool names, prompt tags or chunk ids outside markers.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.adapters.eml import first_name
from app.agents.base import AgentResult, cited_ids
from app.models import Draft, GuardrailVerdict, Inquiry
from app.orchestrator import OutputCheck

MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
CITE_MARKERS = re.compile(r"\s*\[[a-z0-9-]+#[a-z0-9-]+#\d+\]")

# Concrete policy statements (numbers + policy nouns). Kept specific on purpose so
# friendly lines like "I'll send the deposit invoice" are not treated as claims.
POLICY_CLAIMS = [re.compile(rx, re.IGNORECASE) for rx in [
    r"\bnon-?refundable\b",
    r"\brefund(s|ed)?\b(?![^.]*\b(request|question)\b)",
    r"\b\d+%\s+deposit\b|\bdeposit (is|of) \d+%",
    r"\bwithin\s+\d+\s+(hours|days|weeks|business days)\b",
    r"\b\d+\s+hours'?\s+notice\b",
    r"\b(balance|payment) is due\b|\bdue \d+ days before\b|\bdue on the day\b",
    r"\b(late-change|rescheduling|cancellation) fee\b",
    r"\b(free of charge|no (further|extra) charges?)\b",
    r"\b(personal use|commercial) license\b|\braw files\b",
    r"\bvalid for \d+ days\b|\b\d+ months\b",
]]

PROMISES = [re.compile(rx, re.IGNORECASE) for rx in [
    r"\b\d{1,2}%\s*(off|discount)\b",
    r"\b(give|offer|apply|honou?r)(ing)?\s+(you\s+)?(a|an|the|this)?\s*(special\s+)?discount\b",
    r"\bdiscount(ed)? (price|rate|of)\b",
    r"\bprice match\b",
    r"\b(you'?re|you are) (all )?(booked|confirmed|set)\b",
    r"\b(i'?ve|i have|we'?ve|we have) (booked|reserved|confirmed|moved|rescheduled)\b",
    r"\b(booking|date|reservation) (is|has been) (confirmed|reserved|secured|moved|changed)\b",
    r"\b(moved|rescheduled) (you|your [a-z ]+) to\b",
    r"\b(full|your) refund (is|has been|will be) (issued|processed|sent)\b",
    r"\bwaive(d)?\b",
]]

BANNED_PHRASES = ["per our policy", "unfortunately we are unable to", "kindly", "asap", "awesome", "no worries"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
LEAKAGE = re.compile(r"untrusted_email|calculate_quote|check_availability|lookup_distance|save_draft|search_policies|"
                     r"system prompt|tool_results|chunk_id", re.IGNORECASE)
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
# Full names and abbreviations ("Jan 23", "Sept. 4"); customers write both.
_MONTH_RX = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_MENTION = re.compile(
    rf"\b({_MONTH_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RX})(?![a-z])",
    re.IGNORECASE,
)
MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS.split("|"))}


def strip_citations(text: str) -> str:
    """Customer copy: remove [chunk-id] markers that are there for the owner."""
    out = CITE_MARKERS.sub("", text)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", out)


def _numbers(obj: Any) -> set[float]:
    found: set[float] = set()
    if isinstance(obj, bool):
        return found
    if isinstance(obj, (int, float)):
        found.add(round(float(obj), 2))
    elif isinstance(obj, str):
        found |= set(_money_values(obj))  # e.g. "Event Coverage (3 hours x $225.00)"
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= _numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= _numbers(v)
    return found


def _money_values(text: str) -> list[float]:
    return [round(float(m.group(1).replace(",", "") + "." + (m.group(2) or "0")), 2) for m in MONEY.finditer(text)]


def _tool_results(result: AgentResult, name: str | None = None) -> list[dict[str, Any]]:
    return [r.result for r in result.tool_calls
            if isinstance(r.result, dict) and r.result.get("ok") and (name is None or r.name == name)]


def _retrieved_texts(result: AgentResult) -> list[str]:
    return [c.get("text", "") for r in _tool_results(result, "search_policies") for c in r.get("chunks", [])]


def _sentences(body: str) -> list[str]:
    # Move "[id]" markers in front of the sentence-ending punctuation so each
    # marker stays with the sentence it cites, then split.
    norm = re.sub(r"([.!?])((?:\s*\[[a-z0-9-]+#[a-z0-9-]+#\d+\])+)", r"\2\1", body)
    parts = re.split(r"(?<=[.!?])\s+|\n+", norm)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------- checks


def check_amounts(draft: Draft, result: AgentResult) -> GuardrailVerdict:
    allowed = set()
    for r in _tool_results(result):
        if "chunks" not in r:
            allowed |= _numbers(r)
    for text in _retrieved_texts(result):
        allowed |= set(_money_values(text))
    bad = sorted({v for v in _money_values(draft.body) if v not in allowed})
    return GuardrailVerdict(
        name="amounts_match_tools", passed=not bad,
        details=f"unverified amounts: {', '.join(f'${b:,.2f}' for b in bad)}" if bad else "all amounts verified",
    )


def check_citations(draft: Draft, result: AgentResult) -> GuardrailVerdict:
    cited = cited_ids(draft.body)
    unknown = [c for c in cited if c not in result.retrieved_chunk_ids]
    return GuardrailVerdict(
        name="citations_valid", passed=not unknown,
        details=f"not retrieved: {unknown}" if unknown else f"{len(cited)} citation(s), all retrieved",
    )


def check_policy_claims(draft: Draft) -> GuardrailVerdict:
    uncited = []
    for s in _sentences(draft.body):
        if s.lstrip().startswith(("-", "*", "Total", "Deposit")):
            continue  # itemized quote lines come from tools, checked by amounts_match_tools
        if any(p.search(s) for p in POLICY_CLAIMS) and not cited_ids(s):
            uncited.append(s[:80])
    return GuardrailVerdict(
        name="policy_claims_cited", passed=not uncited,
        details=f"uncited policy claims: {uncited}" if uncited else "every policy claim is cited",
    )


def _allowed_dates(inq: Inquiry, result: AgentResult) -> set[tuple[int, int]]:
    allowed: set[tuple[int, int]] = set()
    for m in DATE_MENTION.finditer(f"{inq.subject}\n{inq.body}"):
        allowed.add(_month_day(m))
    for rec in result.tool_calls:
        for v in list(rec.args.values()) + ([rec.result.get("date"), rec.result.get("event_date")] if isinstance(rec.result, dict) else []):
            if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                d = date.fromisoformat(v)
                allowed.add((d.month, d.day))
    for iso in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", inq.body):
        d = date.fromisoformat(iso)
        allowed.add((d.month, d.day))
    return allowed


def _month_day(m: re.Match[str]) -> tuple[int, int]:
    if m.group(1):
        return MONTH_ABBR[m.group(1)[:3].lower()], int(m.group(2))
    return MONTH_ABBR[m.group(4)[:3].lower()], int(m.group(3))


# A promise inside a conditional or negated sentence ("your date is reserved once
# the deposit is received", "we do not offer discounts") is a policy, not a promise.
CONDITIONAL = re.compile(r"\b(once|when|after|until|only|if|not|n't|never|no longer|before)\b", re.IGNORECASE)


def check_promises(inq: Inquiry, draft: Draft, result: AgentResult) -> GuardrailVerdict:
    problems = []
    for sentence in _sentences(draft.body):
        if CONDITIONAL.search(sentence):
            continue
        for p in PROMISES:
            m = p.search(sentence)
            if m:
                problems.append(f"promise: '{m.group(0)}'")
    for r in _tool_results(result, "check_availability"):
        if r.get("status") != "available":
            d = date.fromisoformat(r["date"])
            day_rx = rf"({d:%B}|{d:%b})\.?\s+{d.day}\b[^.]*\b(is|are)\s+(currently\s+)?(open|available|free)\b"
            if re.search(day_rx, draft.body, re.IGNORECASE):
                problems.append(f"says {r['date']} is available but calendar says {r['status']}")
    allowed = _allowed_dates(inq, result)
    for m in DATE_MENTION.finditer(draft.body):
        if _month_day(m) not in allowed:
            problems.append(f"date not from customer or tools: '{m.group(0)}'")
    return GuardrailVerdict(
        name="no_unconfirmed_promises", passed=not problems,
        details="; ".join(problems) if problems else "no unconfirmed promises",
    )


def check_tone(inq: Inquiry, draft: Draft, result: AgentResult) -> GuardrailVerdict:
    body = strip_citations(draft.body)
    issues = []
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    name = first_name(inq.sender)
    if not re.match(rf"Hi ({re.escape(name)}|there)\b" if name else r"Hi there\b", first_line):
        issues.append(f"greeting should be 'Hi {name or 'there'},'")
    if "Warmly," not in body or "Maya" not in body:
        issues.append("missing sign-off 'Warmly, Maya'")
    words = len(body.split())
    limit = 320 if any(r.name == "calculate_quote" for r in result.tool_calls) else 260
    if words > limit:
        issues.append(f"{words} words > {limit}")
    if body.count("!") > 2:
        issues.append(f"{body.count('!')} exclamation marks > 2")
    lowered = body.lower()
    issues += [f"banned phrase '{p}'" for p in BANNED_PHRASES if re.search(rf"\b{re.escape(p)}\b", lowered)]
    if EMOJI.search(body):
        issues.append("contains emoji")
    return GuardrailVerdict(name="tone_and_length", passed=not issues,
                            details="; ".join(issues) if issues else f"{words} words, voice rules met")


def check_leakage(draft: Draft) -> GuardrailVerdict:
    m = LEAKAGE.search(strip_citations(draft.body) + "\n" + draft.subject)
    return GuardrailVerdict(name="no_internal_leakage", passed=m is None,
                            details=f"found '{m.group(0)}'" if m else "clean")


def run_output_checks(inq: Inquiry, draft: Draft, result: AgentResult) -> list[GuardrailVerdict]:
    return [
        check_amounts(draft, result),
        check_citations(draft, result),
        check_policy_claims(draft),
        check_promises(inq, draft, result),
        check_tone(inq, draft, result),
        check_leakage(draft),
    ]


async def output_guard(inq: Inquiry, draft: Draft, result: AgentResult) -> OutputCheck:
    verdicts = run_output_checks(inq, draft, result)
    return OutputCheck(passed=all(v.passed for v in verdicts), verdicts=verdicts)
