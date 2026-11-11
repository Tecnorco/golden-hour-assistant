"""Owner approval screen (Streamlit).

Run from the project folder:  make ui   (or: uv run streamlit run ui/streamlit_app.py)

This process never calls the model, so it never holds the API key: .env is not
loaded and GEMINI_API_KEY is removed from its environment. Its only powers are
reading the store, recording decisions and calling the send gate.

Navigation state lives in st.session_state ("page", "selected") and is only
changed inside button callbacks, which run before any widget is drawn.
All customer text is escaped before it is placed in HTML (see ui_kit.theme.esc).
"""

from __future__ import annotations

import os

os.environ["GHA_SKIP_DOTENV"] = "1"
os.environ.pop("GEMINI_API_KEY", None)

import json  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any  # noqa: E402

import streamlit as st  # noqa: E402

from app.adapters.eml import EmlAdapter  # noqa: E402
from app.agents.base import cited_ids  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.guardrails.output import strip_citations  # noqa: E402
from app.models import Decision, Draft, InquiryStatus  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.orchestrator import InvalidTransition, SendBlocked, record_decision, send_reply, transition  # noqa: E402
from app.store import Store  # noqa: E402
from app.ui_kit.theme import (  # noqa: E402
    CSS, IN_PROGRESS, MUTED, PRESET_DOT_CSS, PRESETS, STATUS, button_icon_css, card_title, display_name, esc, icon,
    route_chip, status_chip,
)

DECIDABLE = {"PENDING_APPROVAL", "OWNER_QUEUE"}
COLS = [160, 440, 195, 115, 105]  # inbox columns: status, inquiry, type, received, open
PACKAGE_NAMES = {
    "mini_portrait": "Mini portrait session", "standard_portrait": "Standard portrait session",
    "event": "Event coverage", "wedding_half_day": "Wedding, half day", "wedding_full_day": "Wedding, full day",
    "unknown": "Not clear yet",
}
FIELD_LABELS = [("service_type", "Package"), ("event_date", "Date"), ("location", "Location"), ("hours", "Hours"),
                ("add_ons", "Add-ons"), ("extra_photo_sets", "Extra photo sets"), ("people_count", "People")]

st.set_page_config(page_title="Golden Hour · Inbox", layout="wide", initial_sidebar_state="expanded")


@st.cache_resource
def _store(path: str) -> Store:
    return Store(path)


settings = get_settings()
store = _store(str(settings.db_path))
tracing.set_sink(store)

st.session_state.setdefault("page", "inbox")
st.session_state.setdefault("preset", "Needs review")

# ---------------------------------------------------------------- callbacks (run before widgets are drawn)


def go(page: str) -> None:
    st.session_state["page"] = page


def open_inquiry(inquiry_id: str) -> None:
    st.session_state["selected"] = inquiry_id
    st.session_state["detail_pick"] = inquiry_id
    st.session_state["page"] = "detail"


def pick_changed() -> None:
    st.session_state["selected"] = st.session_state["detail_pick"]


# ---------------------------------------------------------------- styles

page = st.session_state["page"]
st.html(
    "<style>" + CSS + PRESET_DOT_CSS
    + button_icon_css(".st-key-nav_inbox", "inbox") + button_icon_css(".st-key-nav_detail", "file-text")
    + button_icon_css('[class*="st-key-open_"]', "arrow-right", after=True)
    + button_icon_css(".st-key-back", "arrow-left")
    + button_icon_css(".st-key-approve", "check") + button_icon_css(".st-key-edit_approve", "pencil-line")
    + button_icon_css(".st-key-reject", "x") + button_icon_css(".st-key-unblock", "undo-2")
    + button_icon_css(".st-key-to_owner", "user-round")
    + f".st-key-nav_{page} button{{background:#FFFFFF !important;color:#18181B !important;"
    "box-shadow:0 1px 2px rgba(24,24,27,.06),0 0 0 1px #ECE8E1}"
    + "</style>"
)

rows = store.list_inquiries()
counts: dict[str, int] = {}
for r in rows:
    counts[r["status"]] = counts.get(r["status"], 0) + 1


def when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %H:%M")
    except ValueError:
        return iso[:16]


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.html('<div class="gh-brand">Golden Hour</div><div class="gh-brand-sub">Reply assistant</div>')
    st.button("Inbox", key="nav_inbox", on_click=go, args=("inbox",), use_container_width=True)
    st.button("Inquiry", key="nav_detail", on_click=go, args=("detail",), use_container_width=True,
              disabled=not rows)

    merged: dict[str, int] = {}
    for status, n in counts.items():
        label = STATUS.get(status).label if status in STATUS else status
        merged[label] = merged.get(label, 0) + n
    stats = []
    seen: set[str] = set()
    for status, style in STATUS.items():
        if style.label in merged and style.label not in seen:
            seen.add(style.label)
            stats.append(f'<div class="gh-stat">{icon(style.icon, 15, style.color)}<span>{esc(style.label)}</span>'
                         f'<span class="gh-count">{merged[style.label]}</span></div>')
    st.html('<div class="gh-section">Overview</div>' + ("".join(stats) or '<div class="gh-stat gh-muted">No inquiries yet</div>')
            + f'<div class="gh-footnote">{icon("shield-check", 14, MUTED)}<span>Nothing is sent without your approval.</span></div>')

# ---------------------------------------------------------------- inbox page


def render_inbox() -> None:
    st.html(f'<div class="gh-eyebrow">{icon("inbox", 13)}Inbox</div><div class="gh-title">Customer inquiries</div>'
            '<div class="gh-subtitle">Review the drafted replies. Approve, edit or reject — nothing goes out on its own.</div>')

    def label(name: str) -> str:
        statuses = PRESETS[name][0]
        n = sum(counts.get(s, 0) for s in statuses)
        return f"{name}  {n}" if name != "All" else f"All  {len(rows)}"

    st.pills("Filter", list(PRESETS), key="preset", format_func=label, label_visibility="collapsed")
    chosen = st.session_state.get("preset") or "Needs review"
    shown = [r for r in rows if r["status"] in PRESETS[chosen][0]]

    with st.container(key="card_inbox"):
        if not shown:
            st.html(f'<div class="gh-empty">{icon("inbox", 28, "#D4D4D8", 1.5)}<br><br>'
                    + ("Nothing here yet. Process emails with <code>uv run python -m app.cli run</code>." if not rows
                       else "Nothing in this view.") + "</div>")
            return
        with st.container(key="list_head"):
            for col, text in zip(st.columns(COLS, vertical_alignment="center"), ["Status", "Inquiry", "Type", "Received", ""]):
                col.html(f'<div class="gh-list-head">{text}</div>')
        for r in shown:
            inq = store.get_inquiry(r["id"])
            route = json.loads(r["route"]) if r["route"] else {}
            name, _ = display_name(inq.sender if inq else "")
            note = r["owner_note"] or ""
            if r["status"] in IN_PROGRESS:
                note = note or "Processing was interrupted"
            with st.container(key=f"row_{r['id']}"):
                c1, c2, c3, c4, c5 = st.columns(COLS, vertical_alignment="center")
                c1.html(status_chip(r["status"]))
                c2.html(f'<div class="gh-subject">{esc(inq.subject if inq else r["id"])}</div>'
                        f'<div class="gh-from">{esc(name)}</div>'
                        + (f'<div class="gh-note">{icon("circle-alert", 13, "#C2410C")}{esc(note[:110])}</div>' if note else ""))
                c3.html(route_chip(route.get("label"), route.get("confidence")) if route else '<span class="gh-faint">—</span>')
                c4.html(f'<span class="gh-date">{esc(when(r["received_at"]))}</span>')
                c5.button("Open", key=f"open_{r['id']}", on_click=open_inquiry, args=(r["id"],))


# ---------------------------------------------------------------- detail page


def kv(pairs: list[tuple[str, str]]) -> str:
    return '<dl class="gh-kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"


def fmt_field(key: str, value: Any) -> str:
    if value in (None, [], 0) and key != "event_date":
        return '<span class="gh-faint">—</span>'
    if value is None:
        return '<span class="gh-faint">—</span>'
    if key == "service_type":
        return esc(PACKAGE_NAMES.get(value, value))
    if key == "add_ons":
        return " ".join(f'<span class="gh-chip gh-chip-plain">{esc(str(a).replace("_", " "))}</span>' for a in value)
    if key == "event_date":
        try:
            return esc(datetime.fromisoformat(value).strftime("%A, %B %d, %Y"))
        except ValueError:
            return esc(value)
    return esc(value)


def money(amount: float) -> str:
    return esc(f"${amount:,.2f}")


def quote_table(result: dict[str, Any]) -> str:
    lines = "".join(f"<tr><td>{esc(line['description'])}</td><td>{money(line['amount'])}</td></tr>"
                    for line in result.get("lines", []))
    deposit_pct = esc(result.get("deposit_pct", 30))
    return (f'<table class="gh-table">{lines}<tr class="gh-total"><td>Total</td><td>{money(result["total"])}</td></tr>'
            f'<tr><td class="gh-muted">Deposit ({deposit_pct}%)</td><td class="gh-muted">{money(result["deposit"])}</td></tr></table>')


def render_tools(draft: Draft | None) -> None:
    calls = [t for t in (draft.tool_results if draft else []) if t.name not in ("search_policies", "save_draft")]
    if not calls:
        st.html('<div class="gh-muted">No tools were called for this reply.</div>')
        return
    blocks = []
    for t in calls:
        ok = t.error is None
        args = ", ".join(f"{k}={v}" for k, v in t.args.items()) or "no arguments"
        summary = ""
        if t.name == "calculate_quote" and ok and isinstance(t.result, dict):
            summary = quote_table(t.result)
        elif isinstance(t.result, dict):
            keep = {k: v for k, v in t.result.items() if k not in ("ok", "packages", "add_ons")}
            pairs = " · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in keep.items())
            if t.name == "list_packages":
                pairs = f"{len(t.result.get('packages', []))} packages, {len(t.result.get('add_ons', []))} add-ons"
            summary = f'<div class="gh-row-sub">{esc(pairs[:300])}</div>'
        blocks.append(f'<div class="gh-row">{icon("wrench", 15, MUTED)}<div class="gh-row-main">'
                f'<div class="gh-row-title">{esc(t.name)} {"" if ok else "<span style=color:#DC2626>· failed</span>"}</div>'
                f'<div class="gh-row-sub gh-mono">{esc(args)}</div>'
                + (f'<div class="gh-row-sub" style="color:#DC2626">{esc(t.error)}</div>' if t.error else "")
                + f"{summary}</div></div>")
    st.html("".join(blocks))


def render_detail() -> None:
    if not rows:
        st.html('<div class="gh-empty">No inquiries yet.</div>')
        return
    ids = [r["id"] for r in rows]
    selected = st.session_state.get("selected")
    if selected not in ids:
        selected = ids[0]
        st.session_state["selected"] = selected
        st.session_state["detail_pick"] = selected
    st.session_state.setdefault("detail_pick", selected)

    top_l, top_r = st.columns([1, 2], vertical_alignment="center")
    top_l.button("Inbox", key="back", type="tertiary", on_click=go, args=("inbox",))

    def pick_label(i: str) -> str:
        r = next(x for x in rows if x["id"] == i)
        inq = store.get_inquiry(i)
        return f"{STATUS[r['status']].label if r['status'] in STATUS else r['status']} · {inq.subject if inq else i}"

    top_r.selectbox("Inquiry", ids, key="detail_pick", format_func=pick_label, on_change=pick_changed,
                    label_visibility="collapsed")

    row = store.get_row(selected)
    inq = store.get_inquiry(selected)
    assert row is not None and inq is not None
    status = row["status"]
    draft = store.latest_draft(selected)
    route = json.loads(row["route"]) if row["route"] else None
    extracted = json.loads(row["extracted"]) if row["extracted"] else None
    verdicts = json.loads(row["verdicts"]) if row["verdicts"] else []
    name, addr = display_name(inq.sender)

    st.html(f'<div class="gh-title" style="margin-top:14px">{esc(inq.subject or "(no subject)")}</div>'
            f'<div class="gh-meta">{status_chip(status)}'
            + (route_chip(route["label"], route["confidence"]) if route else "")
            + f'<span>{icon("mail", 14, MUTED)} {esc(name)}</span><span class="gh-faint">·</span>'
            f'<span>{esc(inq.received_at.strftime("%a %b %d, %Y · %H:%M"))}</span></div>')

    if "flash" in st.session_state:
        st.html(f'<div class="gh-callout gh-callout-ok">{icon("circle-check", 16)}<div>{esc(st.session_state.pop("flash"))}</div></div>')
    note = row["owner_note"]
    if status in IN_PROGRESS:
        note = ("Processing was interrupted before a draft was ready (for example the run was stopped). "
                "Re-run this email, or move it to the owner queue to write the reply yourself.")
    if note:
        st.html(f'<div class="gh-callout gh-callout-warn">{icon("circle-alert", 16)}<div><b>Needs your attention.</b> {esc(note)}</div></div>')

    left, right = st.columns([1.15, 1], gap="medium")
    with left, st.container(key="card_email"):
        attach = "".join(f'<span class="gh-chip gh-chip-plain">{icon("paperclip", 12, MUTED)}{esc(a.filename)}</span> '
                         for a in inq.attachments)
        st.html(card_title("mail", "Customer email")
                + f'<div class="gh-row-sub" style="margin:-4px 0 10px">{esc(name)} {f"&lt;{esc(addr)}&gt;" if addr and addr != name else ""}</div>'
                + (f'<div style="margin-bottom:10px">{attach}</div>' if attach else "")
                + f'<div class="gh-email">{esc(inq.body)}</div>')
    with right, st.container(key="card_understanding"):
        parts = card_title("sparkles", "What the assistant understood")
        if route:
            parts += kv([("Type", route_chip(route["label"])),
                         ("Confidence", f'{route["confidence"]:.0%}<div class="gh-bar"><span style="width:{route["confidence"] * 100:.0f}%"></span></div>'),
                         ("Why", f'<span class="gh-muted">{esc(route["reason"])}</span>')])
        else:
            failed_checks = [v for v in verdicts if not v["passed"]]
            parts += '<div class="gh-muted" style="margin-bottom:8px;font-size:13.5px">Stopped by the safety checks before any agent saw it.</div>'
            parts += "".join(
                f'<div class="gh-row">{icon("shield-x", 15, "#DC2626")}<div class="gh-row-main"><div class="gh-row-title">'
                f'{esc(v["name"].replace("_", " ").capitalize())}</div><div class="gh-row-sub">{esc(v["details"])}</div></div></div>'
                for v in failed_checks)
        if extracted:
            parts += '<div class="gh-section" style="margin-top:16px">Details</div>'
            parts += kv([(lbl, fmt_field(k, extracted.get(k))) for k, lbl in FIELD_LABELS])
        st.html(parts)

    # ---- draft and decision
    with st.container(key="card_draft"):
        badge = (f'<span class="gh-chip">{icon("circle-alert", 12, "#F59E0B")}Asks for missing details</span>'
                 if draft and draft.needs_info else "")
        st.html(card_title("pencil-line", "Draft reply", badge))
        subject = draft.subject if draft else f"Re: {inq.subject}"
        original = draft.body if draft else ""
        tab_edit, tab_preview = st.tabs(["Edit", "Customer will receive"])
        with tab_edit:
            edited = st.text_area("Reply", original, height=340, key=f"edit_{selected}", label_visibility="collapsed",
                                  help="Markers like [policy-id] show where a sentence comes from; they are removed before sending.")
        with tab_preview:
            st.html(f'<div class="gh-row-sub" style="margin-bottom:8px">Subject: {esc(subject)}</div>'
                    f'<div class="gh-email" style="max-height:none;background:#fff">{esc(strip_citations(edited))}</div>')
        note_text = st.text_input("Note", key=f"note_{selected}", placeholder="Optional note, saved with your decision",
                                  label_visibility="collapsed")
        can_decide = status in DECIDABLE
        b1, b2, b3, b4 = st.columns([1.1, 1.35, 0.8, 1.6])
        approve = b1.button("Approve & send", key="approve", type="primary", disabled=not (can_decide and draft))
        edit_approve = b2.button("Edit, approve & send", key="edit_approve", disabled=not can_decide)
        reject = b3.button("Reject", key="reject", type="tertiary", disabled=not can_decide)
        unblock = to_owner = False
        if status == "BLOCKED":
            unblock = b4.button("Review blocked item", key="unblock")
        elif status in IN_PROGRESS:
            to_owner = b4.button("Move to owner queue", key="to_owner")

    adapter = EmlAdapter(outbox_dir=settings.outbox_dir)
    try:
        if approve:
            if edited.strip() != original.strip():
                st.error("You changed the text. Use **Edit, approve & send** so your edits are what gets sent.")
            else:
                record_decision(store, Decision(inquiry_id=selected, action="approve", note=note_text))
                path = send_reply(store, adapter, selected)
                st.session_state["flash"] = f"Approved and sent. Saved to {path}"
                st.rerun()
        if edit_approve:
            record_decision(store, Decision(inquiry_id=selected, action="edit_approve", edited_body=edited, note=note_text))
            path = send_reply(store, adapter, selected)
            st.session_state["flash"] = f"Your edited reply was approved and sent. Saved to {path}"
            st.rerun()
        if reject:
            record_decision(store, Decision(inquiry_id=selected, action="reject", note=note_text))
            st.session_state["flash"] = "Rejected. Nothing was sent."
            st.rerun()
        if unblock:
            transition(store, selected, InquiryStatus.OWNER_QUEUE, "owner pulled blocked item for review")
            st.rerun()
        if to_owner:
            transition(store, selected, InquiryStatus.OWNER_QUEUE, "owner took over an interrupted item")
            st.rerun()
    except (SendBlocked, InvalidTransition, ValueError) as exc:
        st.error(str(exc))

    # ---- evidence
    retrieved: list[dict[str, Any]] = []
    for t in (draft.tool_results if draft else []):
        if t.name == "search_policies" and isinstance(t.result, dict):
            retrieved += t.result.get("chunks", [])
    unique = {c["chunk_id"]: c for c in retrieved}
    cited = set(cited_ids(draft.body)) if draft else set()
    failed = sum(not v["passed"] for v in verdicts)
    tools_n = len([t for t in (draft.tool_results if draft else []) if t.name not in ("search_policies", "save_draft")])

    with st.container(key="card_evidence"):
        st.html(card_title("file-search", "Evidence"))
        t_src, t_tools, t_guard, t_time = st.tabs([
            f"Sources · {len(cited)} cited", f"Tool calls · {tools_n}",
            f"Safety checks · {'all passed' if verdicts and not failed else f'{failed} failed' if failed else '—'}",
            "Timeline"])
        with t_src:
            if not unique:
                st.html('<div class="gh-muted">No policy documents were used for this reply.</div>')
            src_rows = []
            for cid, c in unique.items():
                tag = ('<span class="gh-chip"><span class="gh-dot" style="background:#16A34A"></span>cited</span>'
                       if cid in cited else '<span class="gh-chip gh-chip-plain">not cited</span>')
                src_rows.append(f'<div class="gh-row">{icon("book-open", 15, MUTED)}<div class="gh-row-main">'
                                f'<div class="gh-row-title">{esc(c.get("heading", ""))} {tag}</div>'
                                f'<div class="gh-row-sub gh-mono">{esc(cid)}</div>'
                                f'<div class="gh-row-sub">{esc(c.get("text", "")[:420])}</div></div></div>')
            if src_rows:
                st.html("".join(src_rows))
        with t_tools:
            render_tools(draft)
        with t_guard:
            if not verdicts:
                st.html('<div class="gh-muted">No safety checks recorded.</div>')
            guard_rows = []
            for v in verdicts:
                mark = icon("circle-check", 16, "#16A34A") if v["passed"] else icon("circle-x", 16, "#DC2626")
                guard_rows.append(f'<div class="gh-row">{mark}<div class="gh-row-main"><div class="gh-row-title">'
                                  f'{esc(v["name"].replace("_", " ").capitalize())}</div><div class="gh-row-sub">{esc(v["details"])}</div></div></div>')
            if guard_rows:
                st.html("".join(guard_rows))
        with t_time:
            items = [(t["at"], f'{STATUS[t["to_status"]].label if t["to_status"] in STATUS else t["to_status"]}',
                      t["reason"] or "", STATUS[t["to_status"]].color if t["to_status"] in STATUS else MUTED)
                     for t in store.transitions(selected)]
            for d in store.decisions(selected):
                dec = Decision.model_validate_json(d["data"])
                items.append((d["decided_at"], f"Owner decision: {dec.action.replace('_', ' ')}", dec.note, "#18181B"))
            html_items = "".join(
                f'<div class="gh-row"><span class="gh-dot" style="background:{color};margin-top:7px"></span>'
                f'<div class="gh-row-main"><div class="gh-row-title">{esc(title)}</div>'
                f'<div class="gh-row-sub">{esc(when(at))}{" · " + esc(reason) if reason else ""}</div></div></div>'
                for at, title, reason, color in sorted(items, key=lambda x: x[0]))
            spans = store.spans_for_trace(selected)
            root = next((s for s in spans if s.parent_id is None and s.name == "pipeline"), None)
            if root:
                html_items += (f'<div class="gh-row">{icon("history", 15, MUTED)}<div class="gh-row-main"><div class="gh-row-sub">'
                               f'Processing took {root.latency_ms / 1000:.1f}s · {root.tokens_in + root.tokens_out:,} tokens · '
                               f'est. {esc("$" + format(root.cost_usd, ".4f"))} · {len(spans)} trace steps</div></div></div>')
            st.html(html_items)


if page == "detail":
    render_detail()
else:
    render_inbox()
