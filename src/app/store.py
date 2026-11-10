"""SQLite persistence (standard library only).

Rows hold JSON-serialised pydantic models so the schema stays small and the
models stay the single source of truth for shape.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.models import Draft, Inquiry, TraceSpan, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS inquiries (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    data TEXT NOT NULL,          -- Inquiry json
    route TEXT,                  -- RouteDecision json
    extracted TEXT,              -- ExtractedFields json
    verdicts TEXT,               -- list[GuardrailVerdict] json
    owner_note TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL           -- Draft json
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    data TEXT NOT NULL           -- Decision json
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_id TEXT,
    name TEXT NOT NULL,
    start TEXT NOT NULL,
    latency_ms REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    error TEXT,
    data TEXT NOT NULL           -- TraceSpan json
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS drafts_inq ON drafts(inquiry_id);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Streamlit and the async pipeline may touch the store from different threads.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------ inquiries

    def add_inquiry(self, inq: Inquiry, status: str = "RECEIVED") -> None:
        now = utcnow().isoformat()
        self._exec(
            "INSERT OR REPLACE INTO inquiries (id, status, received_at, data, updated_at) VALUES (?,?,?,?,?)",
            (inq.id, status, inq.received_at.isoformat(), inq.model_dump_json(), now),
        )
        self._exec(
            "INSERT INTO transitions (inquiry_id, from_status, to_status, at, reason) VALUES (?,?,?,?,?)",
            (inq.id, None, status, now, "received"),
        )

    def get_inquiry(self, inquiry_id: str) -> Inquiry | None:
        rows = self._query("SELECT data FROM inquiries WHERE id=?", (inquiry_id,))
        return Inquiry.model_validate_json(rows[0]["data"]) if rows else None

    def get_row(self, inquiry_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
        return dict(rows[0]) if rows else None

    def get_status(self, inquiry_id: str) -> str | None:
        rows = self._query("SELECT status FROM inquiries WHERE id=?", (inquiry_id,))
        return rows[0]["status"] if rows else None

    def set_status(self, inquiry_id: str, from_status: str | None, to_status: str, reason: str = "") -> None:
        """Low-level write. State-machine rules live in the orchestrator, not here."""
        now = utcnow().isoformat()
        self._exec("UPDATE inquiries SET status=?, updated_at=? WHERE id=?", (to_status, now, inquiry_id))
        self._exec(
            "INSERT INTO transitions (inquiry_id, from_status, to_status, at, reason) VALUES (?,?,?,?,?)",
            (inquiry_id, from_status, to_status, now, reason),
        )

    def set_field(self, inquiry_id: str, column: str, value: Any) -> None:
        if column not in {"route", "extracted", "verdicts", "owner_note"}:
            raise ValueError(f"unknown column {column}")
        payload = value if isinstance(value, str) or value is None else json.dumps(value, default=str)
        self._exec(f"UPDATE inquiries SET {column}=? WHERE id=?", (payload, inquiry_id))

    def list_inquiries(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._query("SELECT * FROM inquiries WHERE status=? ORDER BY received_at DESC", (status,))
        else:
            rows = self._query("SELECT * FROM inquiries ORDER BY received_at DESC")
        return [dict(r) for r in rows]

    def transitions(self, inquiry_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM transitions WHERE inquiry_id=? ORDER BY id", (inquiry_id,))]

    # ------------------------------------------------------------ drafts / decisions

    def add_draft(self, draft: Draft) -> int:
        cur = self._exec(
            "INSERT INTO drafts (inquiry_id, created_at, data) VALUES (?,?,?)",
            (draft.inquiry_id, utcnow().isoformat(), draft.model_dump_json()),
        )
        return int(cur.lastrowid or 0)

    def latest_draft(self, inquiry_id: str) -> Draft | None:
        rows = self._query("SELECT data FROM drafts WHERE inquiry_id=? ORDER BY id DESC LIMIT 1", (inquiry_id,))
        return Draft.model_validate_json(rows[0]["data"]) if rows else None

    def add_decision_json(self, inquiry_id: str, decided_at: str, data: str) -> None:
        self._exec("INSERT INTO decisions (inquiry_id, decided_at, data) VALUES (?,?,?)", (inquiry_id, decided_at, data))

    def decisions(self, inquiry_id: str | None = None) -> list[dict[str, Any]]:
        if inquiry_id:
            return [dict(r) for r in self._query("SELECT * FROM decisions WHERE inquiry_id=? ORDER BY id", (inquiry_id,))]
        return [dict(r) for r in self._query("SELECT * FROM decisions ORDER BY id")]

    # ------------------------------------------------------------ spans

    def add_span(self, span: TraceSpan) -> None:
        self._exec(
            "INSERT OR REPLACE INTO spans (span_id, trace_id, parent_id, name, start, latency_ms, tokens_in, "
            "tokens_out, cost_usd, error, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                span.span_id, span.trace_id, span.parent_id, span.name, span.start.isoformat(),
                span.latency_ms, span.tokens_in, span.tokens_out, span.cost_usd, span.error,
                span.model_dump_json(),
            ),
        )

    def spans_for_trace(self, trace_id: str) -> list[TraceSpan]:
        rows = self._query("SELECT data FROM spans WHERE trace_id=? ORDER BY start", (trace_id,))
        return [TraceSpan.model_validate_json(r["data"]) for r in rows]

    def all_spans(self) -> list[TraceSpan]:
        return [TraceSpan.model_validate_json(r["data"]) for r in self._query("SELECT data FROM spans ORDER BY start")]
