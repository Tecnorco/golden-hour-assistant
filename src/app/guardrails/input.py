"""Input guardrails: run before any agent sees an email.

1. Length limit: very long bodies are truncated (and flagged) so no one can
   bury instructions in 50 KB of text or run up costs.
2. Prompt-injection detection, two layers:
   - heuristics: high-precision regexes for known attack phrasing;
   - LLM classifier: catches paraphrased attacks the regexes miss.
   Either one blocks. Blocked emails never reach the router or the agents.
3. Spam filter: a keyword score; obvious spam is blocked before any LLM spend
   on drafting (the router's "spam" label is a second net).
PII never appears in verdict details: evidence is redacted.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.agents.common import render_untrusted
from app.config import Settings
from app.guardrails.redact import redact
from app.llm.client import LLM, LLMError
from app.llm.prompts import load_prompt
from app.models import GuardrailVerdict, Inquiry
from app.observability.tracing import span
from app.orchestrator import InputCheck, InputGuard

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in [
        ("ignore_instructions", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instructions|rules|prompt|guidelines|pricing rules|system)\b"),
        ("system_override", r"\b(system\s*override|developer\s*mode|jailbreak|admin\s*mode|god\s*mode)\b"),
        ("role_reassignment", r"\byou are now\b|\bact as (an?|the) (ai|assistant|system)\b|\bnew instructions?\s*:"),
        ("addressed_to_ai", r"\b(note|message|instructions?)\s+(to|for)\s+(the\s+)?(ai|assistant|bot|model|llm|system)\b"),
        ("ai_processing_this", r"\b(ai|assistant|bot|model|llm)\b[^.\n]{0,30}\b(processing|reading|handling)\s+this\b"),
        ("prompt_exfiltration", r"\b(reveal|print|show|repeat|output)\b[^.\n]{0,30}\b(system prompt|your prompt|instructions|api keys?|secrets?)\b"),
        ("fake_preapproval", r"\b(owner|maya|management)\s+(has\s+)?(pre-?approved|authori[sz]ed)\b"),
        ("skip_approval", r"\b(without|skip|bypass|do not ask)\b[^.\n]{0,30}\b(approval|the owner|review)\b"),
        ("delimiter_escape", r"</?\s*(untrusted_email|system|instructions?)\s*>"),
    ]
]

SPAM_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in [
        ("guarantee", r"\bguarantee(d|s)?\b"),
        ("seo", r"\b(seo|rank(ing)? #?1|page 1 of google|backlinks)\b"),
        ("click_here", r"\bclick (here|below|this link)\b"),
        ("percent_off", r"\b\d{2}% off\b"),
        ("limited_time", r"\blimited[- ]time\b|\bact now\b|\bonly today\b"),
        ("unsubscribe", r"\bunsubscribe\b|\breply stop\b"),
        ("generic_greeting", r"\bdear (business owner|sir/madam|website owner)\b"),
        ("crypto_or_loans", r"\b(crypto|bitcoin|loan offer|investment opportunity)\b"),
        ("shouting", r"!!!"),
    ]
]
SPAM_BLOCK_SCORE = 3


class InjectionVerdict(BaseModel):
    is_injection: bool
    confidence: float = Field(ge=0, le=1)
    evidence: str = ""


def check_length(inq: Inquiry, max_chars: int) -> tuple[Inquiry, GuardrailVerdict]:
    if len(inq.body) <= max_chars:
        return inq, GuardrailVerdict(name="input_length", passed=True, details=f"{len(inq.body)} chars")
    truncated = inq.model_copy(update={"body": inq.body[:max_chars] + "\n[... truncated by input guardrail ...]"})
    return truncated, GuardrailVerdict(
        name="input_length", passed=True, details=f"truncated {len(inq.body)} -> {max_chars} chars"
    )


def injection_heuristics(text: str) -> GuardrailVerdict:
    hits = [name for name, rx in INJECTION_PATTERNS if rx.search(text)]
    return GuardrailVerdict(name="injection_heuristics", passed=not hits, details=", ".join(hits) or "no known patterns")


def spam_score(text: str) -> tuple[int, list[str]]:
    hits = [name for name, rx in SPAM_SIGNALS if rx.search(text)]
    links = len(re.findall(r"https?://", text))
    if links >= 2:
        hits.append(f"{links}_links")
    return len(hits), hits


async def injection_classifier(inq: Inquiry, llm: LLM, min_confidence: float) -> GuardrailVerdict:
    prompt = load_prompt("injection_classifier")
    try:
        resp = await llm.generate(
            prompt_name="injection_classifier", system=prompt.text, turns=render_untrusted(inq),
            schema=InjectionVerdict, temperature=0.0,
        )
    except LLMError as exc:
        # Fail closed: if we cannot check, a human looks at it.
        return GuardrailVerdict(name="injection_classifier", passed=False, details=f"classifier unavailable: {exc}"[:200])
    v: InjectionVerdict = resp.parsed
    flagged = v.is_injection and v.confidence >= min_confidence
    return GuardrailVerdict(
        name="injection_classifier", passed=not flagged,
        details=f"is_injection={v.is_injection} conf={v.confidence:.2f} evidence={redact(v.evidence)[:120]!r}",
    )


def make_input_guard(llm: LLM, settings: Settings, classifier_min_confidence: float = 0.8) -> InputGuard:
    async def guard(inq: Inquiry) -> InputCheck:
        clean, length_v = check_length(inq, settings.max_input_chars)
        text = f"{clean.subject}\n{clean.body}"
        verdicts = [length_v]

        score, signals = spam_score(text)
        spam_v = GuardrailVerdict(name="spam_filter", passed=score < SPAM_BLOCK_SCORE,
                                  details=f"score={score} ({', '.join(signals) or 'none'})")
        heur_v = injection_heuristics(text)
        with span("injection_classifier"):
            clf_v = await injection_classifier(clean, llm, classifier_min_confidence)
        verdicts += [heur_v, clf_v, spam_v]

        if not heur_v.passed or not clf_v.passed:
            return InputCheck(allowed=False, reason="prompt injection suspected", verdicts=verdicts)
        if not spam_v.passed:
            return InputCheck(allowed=False, reason="spam", verdicts=verdicts)
        return InputCheck(allowed=True, verdicts=verdicts, sanitized=clean if clean is not inq else None)

    return guard
