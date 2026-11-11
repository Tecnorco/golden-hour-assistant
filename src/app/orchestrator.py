"""Orchestrator: an explicit, persisted state machine with typed hand-offs.

    RECEIVED -> BLOCKED | ROUTED | OWNER_QUEUE
    ROUTED   -> DRAFTED | OWNER_QUEUE | BLOCKED (spam)
    DRAFTED  -> PENDING_APPROVAL | OWNER_QUEUE (output guardrails failed)
    PENDING_APPROVAL | OWNER_QUEUE -> APPROVED | EDITED_APPROVED | REJECTED
    APPROVED | EDITED_APPROVED -> SENT

Each step is a small function taking and returning typed objects so it can be
tested alone. Only this module changes an inquiry's status, and every change
goes through `transition`, which rejects anything not in the table.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.agents.base import AgentDeps, AgentResult
from app.agents.extractor import extract_fields
from app.agents.quote_agent import run_quote_agent
from app.agents.router import needs_owner, route
from app.agents.support_agents import run_faq_agent, run_other_agent
from app.config import Settings
from app.adapters.base import Adapter
from app.models import Decision, Draft, ExtractedFields, GuardrailVerdict, Inquiry, InquiryStatus, OutgoingReply, RouteDecision
from app.observability.tracing import span, trace
from app.store import Store

S = InquiryStatus

TRANSITIONS: dict[InquiryStatus, set[InquiryStatus]] = {
    S.RECEIVED: {S.BLOCKED, S.ROUTED, S.OWNER_QUEUE},
    S.ROUTED: {S.DRAFTED, S.OWNER_QUEUE, S.BLOCKED},
    S.DRAFTED: {S.PENDING_APPROVAL, S.OWNER_QUEUE},
    S.PENDING_APPROVAL: {S.APPROVED, S.EDITED_APPROVED, S.REJECTED},
    S.OWNER_QUEUE: {S.APPROVED, S.EDITED_APPROVED, S.REJECTED},
    S.BLOCKED: {S.OWNER_QUEUE},  # the owner may pull a false positive back for review
    S.APPROVED: {S.SENT},
    S.EDITED_APPROVED: {S.SENT},
    S.REJECTED: set(),
    S.SENT: set(),
}


class InvalidTransition(RuntimeError):
    pass


def transition(store: Store, inquiry_id: str, to: InquiryStatus, reason: str = "") -> None:
    current = store.get_status(inquiry_id)
    if current is None:
        raise InvalidTransition(f"unknown inquiry {inquiry_id}")
    frm = InquiryStatus(current)
    if to not in TRANSITIONS[frm]:
        raise InvalidTransition(f"{inquiry_id}: {frm.value} -> {to.value} is not allowed")
    with span("transition", input={"from": frm.value, "to": to.value, "reason": reason}):
        store.set_status(inquiry_id, frm.value, to.value, reason)


# ---------------------------------------------------------------- guardrail hooks


class InputCheck(BaseModel):
    allowed: bool
    reason: str = ""
    verdicts: list[GuardrailVerdict] = Field(default_factory=list)
    sanitized: Inquiry | None = None  # e.g. truncated body; None = use the original


class OutputCheck(BaseModel):
    passed: bool
    verdicts: list[GuardrailVerdict] = Field(default_factory=list)


InputGuard = Callable[[Inquiry], Awaitable[InputCheck]]
OutputGuard = Callable[[Inquiry, Draft, AgentResult], Awaitable[OutputCheck]]
Repairer = Callable[[Inquiry, Draft, AgentResult, OutputCheck], Awaitable[AgentResult | None]]


async def no_input_guard(inq: Inquiry) -> InputCheck:
    return InputCheck(allowed=True, verdicts=[GuardrailVerdict(name="input_guardrails", passed=True, details="not configured")])


async def no_output_guard(inq: Inquiry, draft: Draft, result: AgentResult) -> OutputCheck:
    return OutputCheck(passed=True, verdicts=[GuardrailVerdict(name="output_guardrails", passed=True, details="not configured")])


# ---------------------------------------------------------------- pipeline


class PipelineOutcome(BaseModel):
    inquiry_id: str
    status: InquiryStatus
    route: RouteDecision | None = None
    agent: str | None = None
    reason: str = ""
    fields: ExtractedFields | None = None
    draft: Draft | None = None
    needs_info: bool = False
    flag_owner: bool = False
    agent_result: AgentResult | None = None
    verdicts: list[GuardrailVerdict] = Field(default_factory=list)


@dataclass
class Pipeline:
    store: Store
    deps: AgentDeps
    settings: Settings
    input_guard: InputGuard = no_input_guard
    output_guard: OutputGuard = no_output_guard
    repair: Repairer | None = None
    log: list[PipelineOutcome] = field(default_factory=list)

    async def process(self, inq: Inquiry) -> PipelineOutcome:
        self.store.add_inquiry(inq)
        with trace(inq.id, name="pipeline", input={"subject": inq.subject, "channel": inq.channel}) as root:
            try:
                outcome = await self._run(inq)
            except Exception as exc:  # noqa: BLE001 - any failure must land in the owner queue, never auto-answer
                reason = f"pipeline error: {type(exc).__name__}: {exc}"
                self._to_owner(inq.id, reason)
                outcome = PipelineOutcome(inquiry_id=inq.id, status=S(self.store.get_status(inq.id)), reason=reason)
            root.output = {"status": outcome.status.value, "reason": outcome.reason}
        self.log.append(outcome)
        return outcome

    def _to_owner(self, inquiry_id: str, reason: str) -> None:
        status = InquiryStatus(self.store.get_status(inquiry_id))
        if S.OWNER_QUEUE in TRANSITIONS[status]:
            transition(self.store, inquiry_id, S.OWNER_QUEUE, reason)
        self.store.set_field(inquiry_id, "owner_note", reason)

    async def _run(self, inq: Inquiry) -> PipelineOutcome:
        out = PipelineOutcome(inquiry_id=inq.id, status=S.RECEIVED)

        # 1. input guardrails
        with span("input_guardrails") as s:
            check = await self.input_guard(inq)
            for v in check.verdicts:
                s.verdict(v)
        out.verdicts += check.verdicts
        if not check.allowed:
            transition(self.store, inq.id, S.BLOCKED, check.reason)
            self._save_verdicts(inq.id, out.verdicts)
            self.store.set_field(inq.id, "owner_note", check.reason)
            return out.model_copy(update={"status": S.BLOCKED, "reason": check.reason})
        clean = check.sanitized or inq

        # 2. routing
        decision = await route(clean, self.deps.llm)
        out.route = decision
        self.store.set_field(inq.id, "route", decision.model_dump())
        if decision.label == "spam":
            transition(self.store, inq.id, S.ROUTED, f"spam ({decision.confidence:.2f})")
            transition(self.store, inq.id, S.BLOCKED, "spam: no reply drafted")
            self.store.set_field(inq.id, "owner_note", f"spam: {decision.reason}")
            return out.model_copy(update={"status": S.BLOCKED, "reason": "spam"})
        if needs_owner(decision, self.settings.router_confidence_threshold):
            reason = f"low routing confidence {decision.confidence:.2f} ({decision.label})"
            self._to_owner(inq.id, reason)
            return out.model_copy(update={"status": S.OWNER_QUEUE, "reason": reason})
        transition(self.store, inq.id, S.ROUTED, f"{decision.label} ({decision.confidence:.2f})")

        # 3. specialist agent
        result = await self._dispatch(clean, decision, out)
        out.agent, out.agent_result, out.flag_owner = result.agent, result, result.flag_owner
        if result.draft is None:
            self._to_owner(inq.id, result.reason)
            return out.model_copy(update={"status": S.OWNER_QUEUE, "reason": result.reason})
        out.draft, out.needs_info = result.draft, result.draft.needs_info
        transition(self.store, inq.id, S.DRAFTED, f"{result.agent} agent")
        if result.status == "needs_owner":
            self._save_verdicts(inq.id, out.verdicts)
            self._to_owner(inq.id, result.reason)
            return out.model_copy(update={"status": S.OWNER_QUEUE, "reason": result.reason})

        # 4. output guardrails, with one automatic repair attempt
        with span("output_guardrails") as s:
            ocheck = await self.output_guard(clean, result.draft, result)
            for v in ocheck.verdicts:
                s.verdict(v)
        if not ocheck.passed and self.repair is not None:
            with span("repair"):
                repaired = await self.repair(clean, result.draft, result, ocheck)
            if repaired is not None and repaired.draft is not None:
                result, out.draft = repaired, repaired.draft
                with span("output_guardrails.retry") as s:
                    ocheck = await self.output_guard(clean, result.draft, result)
                    for v in ocheck.verdicts:
                        s.verdict(v)
        out.verdicts += ocheck.verdicts
        self._save_verdicts(inq.id, out.verdicts)
        if not ocheck.passed:
            failed = ", ".join(v.name for v in ocheck.verdicts if not v.passed)
            reason = f"output guardrails failed: {failed}"
            self._to_owner(inq.id, reason)
            return out.model_copy(update={"status": S.OWNER_QUEUE, "reason": reason})

        transition(self.store, inq.id, S.PENDING_APPROVAL, "awaiting owner approval")
        if result.flag_owner:
            self.store.set_field(inq.id, "owner_note", result.reason)
        return out.model_copy(update={"status": S.PENDING_APPROVAL, "reason": result.reason})

    async def _dispatch(self, inq: Inquiry, decision: RouteDecision, out: PipelineOutcome) -> AgentResult:
        if decision.label == "quote_request":
            fields = await extract_fields(inq, self.deps.llm)
            out.fields = fields
            self.store.set_field(inq.id, "extracted", fields.model_dump(mode="json"))
            return await run_quote_agent(inq, fields, self.deps)
        if decision.label == "question":
            return await run_faq_agent(inq, self.deps)
        return await run_other_agent(inq, self.deps, decision.label)

    def _save_verdicts(self, inquiry_id: str, verdicts: list[GuardrailVerdict]) -> None:
        self.store.set_field(inquiry_id, "verdicts", [v.model_dump() for v in verdicts])


# ---------------------------------------------------------------- human in the loop + send gate


class SendBlocked(RuntimeError):
    """Raised whenever something tries to send a reply without recorded approval."""


SENDABLE = {S.APPROVED, S.EDITED_APPROVED}
ACTION_TO_STATUS = {"approve": S.APPROVED, "edit_approve": S.EDITED_APPROVED, "reject": S.REJECTED}


def record_decision(store: Store, decision: Decision) -> None:
    """Owner decision: logged with timestamp, then a state transition. Edited text
    is stored as a new draft version so the sent text is always on record."""
    status = InquiryStatus(store.get_status(decision.inquiry_id) or "RECEIVED")
    target = ACTION_TO_STATUS[decision.action]
    if target not in TRANSITIONS[status]:
        raise InvalidTransition(f"cannot {decision.action} an inquiry in status {status.value}")
    if decision.action == "edit_approve":
        if not (decision.edited_body or "").strip():
            raise ValueError("edit_approve needs the edited text")
        base = store.latest_draft(decision.inquiry_id)
        inq = store.get_inquiry(decision.inquiry_id)
        store.add_draft(Draft(
            inquiry_id=decision.inquiry_id,
            subject=base.subject if base else f"Re: {inq.subject if inq else ''}",
            body=decision.edited_body or "", sources=base.sources if base else [],
            tool_results=base.tool_results if base else [], agent="owner",
        ))
    with trace(decision.inquiry_id, name="owner_decision", input={"action": decision.action}):
        store.add_decision_json(decision.inquiry_id, decision.decided_at.isoformat(), decision.model_dump_json())
        transition(store, decision.inquiry_id, target, f"owner: {decision.action}{' - ' + decision.note if decision.note else ''}")


def send_reply(store: Store, adapter: Adapter, inquiry_id: str) -> str:
    """The ONLY way a reply leaves the system. Raises SendBlocked unless the
    stored status is APPROVED or EDITED_APPROVED and a matching owner decision is
    on record. Citation markers are stripped from the customer's copy."""
    from app.guardrails.output import strip_citations  # local import: guardrails import this module

    status = store.get_status(inquiry_id)
    if status is None or InquiryStatus(status) not in SENDABLE:
        raise SendBlocked(f"refusing to send {inquiry_id}: status is {status}, not APPROVED/EDITED_APPROVED")
    decisions = [Decision.model_validate_json(d["data"]) for d in store.decisions(inquiry_id)]
    if not decisions or decisions[-1].action not in ("approve", "edit_approve"):
        raise SendBlocked(f"refusing to send {inquiry_id}: no recorded owner approval")
    draft = store.latest_draft(inquiry_id)
    inq = store.get_inquiry(inquiry_id)
    if draft is None or inq is None or not draft.body.strip():
        raise SendBlocked(f"refusing to send {inquiry_id}: no draft on record")
    reply = OutgoingReply(inquiry_id=inquiry_id, thread_id=inq.thread_id, to=inq.sender,
                          subject=draft.subject, body=strip_citations(draft.body))
    with trace(inquiry_id, name="send", input={"channel": inq.channel}) as s:
        ref = adapter.send_reply(reply)
        transition(store, inquiry_id, S.SENT, f"sent via {inq.channel}")
        s.output = "sent"
    return ref
