"""Owner approval screen (Streamlit).

Run:  make ui   (or: streamlit run ui/streamlit_app.py)

This process never calls the model, so it never holds the API key: .env is not
loaded and GEMINI_API_KEY is removed from its environment. Its only powers are
reading the store, recording decisions and calling the send gate.
"""

from __future__ import annotations

import os

os.environ["GHA_SKIP_DOTENV"] = "1"
os.environ.pop("GEMINI_API_KEY", None)

import json  # noqa: E402

import streamlit as st  # noqa: E402

from app.adapters.eml import EmlAdapter  # noqa: E402
from app.agents.base import cited_ids  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.guardrails.output import strip_citations  # noqa: E402
from app.models import Decision, InquiryStatus  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.orchestrator import InvalidTransition, SendBlocked, record_decision, send_reply, transition  # noqa: E402
from app.store import Store  # noqa: E402

ACTIONABLE = ["PENDING_APPROVAL", "OWNER_QUEUE", "BLOCKED"]
DECIDABLE = {"PENDING_APPROVAL", "OWNER_QUEUE"}
BADGE = {
    "PENDING_APPROVAL": "🟡", "OWNER_QUEUE": "🟠", "BLOCKED": "⛔", "APPROVED": "🟢", "EDITED_APPROVED": "🟢",
    "SENT": "✅", "REJECTED": "⚪", "RECEIVED": "·", "ROUTED": "·", "DRAFTED": "·",
}

st.set_page_config(page_title="Golden Hour Assistant", page_icon="📷", layout="wide")


@st.cache_resource
def _store(path: str) -> Store:
    return Store(path)


settings = get_settings()
store = _store(str(settings.db_path))
tracing.set_sink(store)


def load_chunks() -> dict[str, dict]:
    path = settings.index_dir / "chunks.json"
    if not path.exists():
        return {}
    return {c["chunk_id"]: c for c in json.loads(path.read_text(encoding="utf-8"))}


def label(row: dict) -> str:
    inq = store.get_inquiry(row["id"])
    return f"{BADGE.get(row['status'], '')} {row['status']} · {inq.subject if inq else row['id']} · {row['id']}"


# ---------------------------------------------------------------- sidebar

st.sidebar.title("📷 Golden Hour")
page = st.sidebar.radio("Page", ["Queue", "Inquiry detail"], key="page")
counts = {s.value: len(store.list_inquiries(s.value)) for s in InquiryStatus}
st.sidebar.markdown("\n".join(f"- {BADGE[s]} **{s}**: {n}" for s, n in counts.items() if n))
st.sidebar.caption("Nothing is ever sent without your approval.")

rows = store.list_inquiries()

# ---------------------------------------------------------------- queue page

if page == "Queue":
    st.header("Inquiry queue")
    statuses = st.multiselect("Status", [s.value for s in InquiryStatus], default=ACTIONABLE, key="status_filter")
    shown = [r for r in rows if r["status"] in statuses]
    table = []
    for r in shown:
        inq = store.get_inquiry(r["id"])
        route = json.loads(r["route"]) if r["route"] else {}
        table.append({
            "status": f"{BADGE.get(r['status'], '')} {r['status']}",
            "subject": inq.subject if inq else "",
            "route": f"{route.get('label', '-')} ({route.get('confidence', 0):.2f})" if route else "-",
            "note": r["owner_note"] or "",
            "from": inq.sender if inq else "",
            "received": r["received_at"][:16].replace("T", " "),
            "id": r["id"],
        })
    if table:
        st.dataframe(table, hide_index=True, width="stretch")
        options = [r["id"] for r in shown]
        pick = st.selectbox("Open inquiry", options, format_func=lambda i: label(next(r for r in shown if r["id"] == i)),
                            key="queue_pick")
        if st.button("Open", key="open"):
            st.session_state["selected"] = pick
            st.session_state["page"] = "Inquiry detail"
            st.rerun()
    else:
        st.info("Nothing here. Process new emails with `make run`.")

# ---------------------------------------------------------------- detail page

else:
    if not rows:
        st.info("No inquiries yet. Process emails with `make run`.")
        st.stop()
    ids = [r["id"] for r in rows]
    current = st.session_state.get("selected", ids[0])
    selected = st.selectbox("Inquiry", ids, index=ids.index(current) if current in ids else 0,
                            format_func=lambda i: label(next(r for r in rows if r["id"] == i)), key="detail_pick")
    st.session_state["selected"] = selected
    row = store.get_row(selected)
    inq = store.get_inquiry(selected)
    assert row is not None and inq is not None
    status = row["status"]
    draft = store.latest_draft(selected)
    route = json.loads(row["route"]) if row["route"] else None
    extracted = json.loads(row["extracted"]) if row["extracted"] else None
    verdicts = json.loads(row["verdicts"]) if row["verdicts"] else []

    st.header(inq.subject or "(no subject)")
    st.markdown(f"**Status:** {BADGE.get(status, '')} `{status}`")
    if row["owner_note"]:
        st.warning(f"**Needs your attention:** {row['owner_note']}")

    left, right = st.columns(2)
    with left:
        st.subheader("Original email")
        st.caption(f"From {inq.sender} · {inq.received_at:%a %d %b %Y %H:%M}")
        if inq.attachments:
            st.caption("Attachments: " + ", ".join(a.filename for a in inq.attachments))
        st.text_area("Body", inq.body, height=260, disabled=True, key=f"body_{selected}")
    with right:
        st.subheader("Routing")
        if route:
            st.markdown(f"**{route['label']}** · confidence **{route['confidence']:.2f}**")
            st.caption(route["reason"])
        else:
            st.caption("Not routed (stopped by input guardrails).")
        if extracted:
            st.subheader("Extracted fields")
            st.json(extracted, expanded=True)

    # ---- draft + decision
    st.subheader("Draft reply")
    if draft and draft.needs_info:
        st.info("This reply asks the customer for missing details (no quote yet).")
    subject = draft.subject if draft else f"Re: {inq.subject}"
    original = draft.body if draft else ""
    edited = st.text_area("Edit the reply (citation markers [..] are removed before sending)", original,
                          height=320, key=f"edit_{selected}")
    with st.expander("Customer will receive", expanded=False):
        st.text(f"Subject: {subject}\n\n{strip_citations(edited)}")

    note = st.text_input("Note (optional, saved with your decision)", key=f"note_{selected}")
    can_decide = status in DECIDABLE
    c1, c2, c3, c4 = st.columns(4)
    approve = c1.button("✅ Approve & send", disabled=not (can_decide and draft), key="approve")
    edit_approve = c2.button("✏️ Edit, approve & send", disabled=not can_decide, key="edit_approve")
    reject = c3.button("✖ Reject", disabled=not can_decide, key="reject")
    review = c4.button("↩ Review blocked item", disabled=status != "BLOCKED", key="unblock")

    adapter = EmlAdapter(outbox_dir=settings.outbox_dir)
    try:
        if approve:
            if edited.strip() != original.strip():
                st.error("You changed the text. Use **Edit, approve & send** so your edits are what gets sent.")
            else:
                record_decision(store, Decision(inquiry_id=selected, action="approve", note=note))
                st.session_state["flash"] = f"Approved and sent → {send_reply(store, adapter, selected)}"
                st.rerun()
        if edit_approve:
            record_decision(store, Decision(inquiry_id=selected, action="edit_approve", edited_body=edited, note=note))
            st.session_state["flash"] = f"Edited, approved and sent → {send_reply(store, adapter, selected)}"
            st.rerun()
        if reject:
            record_decision(store, Decision(inquiry_id=selected, action="reject", note=note))
            st.session_state["flash"] = "Rejected. Nothing was sent."
            st.rerun()
        if review:
            transition(store, selected, InquiryStatus.OWNER_QUEUE, "owner pulled blocked item for review")
            st.rerun()
    except (SendBlocked, InvalidTransition, ValueError) as exc:
        st.error(str(exc))
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    # ---- evidence
    chunks = load_chunks()
    with st.expander(f"Retrieved sources ({len(draft.sources) if draft else 0} cited)"):
        retrieved = []
        for t in (draft.tool_results if draft else []):
            if t.name == "search_policies" and isinstance(t.result, dict):
                retrieved += [c["chunk_id"] for c in t.result.get("chunks", [])]
        cited = set(cited_ids(draft.body)) if draft else set()
        if not retrieved:
            st.caption("No policy chunks were retrieved.")
        for cid in dict.fromkeys(retrieved):
            c = chunks.get(cid, {})
            st.markdown(f"{'**[cited]**' if cid in cited else '[not cited]'} `{cid}` — {c.get('heading', '')}")
            st.caption(c.get("text", "")[:600])
    with st.expander(f"Tool calls ({len(draft.tool_results) if draft else 0})"):
        for t in (draft.tool_results if draft else []):
            if t.name == "search_policies":
                continue
            st.markdown(f"**{t.name}** {'❌ ' + t.error if t.error else ''}")
            st.json({"args": t.args, "result": t.result}, expanded=t.name == "calculate_quote")
    with st.expander(f"Guardrail verdicts ({sum(not v['passed'] for v in verdicts)} failed)"):
        for v in verdicts:
            st.markdown(f"{'✅' if v['passed'] else '❌'} **{v['name']}** — {v['details']}")
    with st.expander("Timeline"):
        for t in store.transitions(selected):
            st.markdown(f"`{t['at'][:19]}` {t['from_status'] or '∅'} → **{t['to_status']}** — {t['reason'] or ''}")
        for d in store.decisions(selected):
            dec = Decision.model_validate_json(d["data"])
            st.markdown(f"`{d['decided_at'][:19]}` owner decision: **{dec.action}** {('— ' + dec.note) if dec.note else ''}")
        spans = store.spans_for_trace(selected)
        root = [s for s in spans if s.parent_id is None and s.name == "pipeline"]
        if root:
            r = root[0]
            st.caption(f"Pipeline: {r.latency_ms / 1000:.1f}s · {r.tokens_in + r.tokens_out} tokens · "
                       f"${r.cost_usd:.4f} · {len(spans)} spans  (CLI: python -m app.cli trace {selected})")
