"""Deciding which email files are new, so nothing is processed (or paid for) twice.

Each email has a `source_key`: its Message-ID header, or a hash of the file when
the header is missing. An email whose key is already in the database is skipped.
Used by `run`, `watch` and `demo-snapshot`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.eml import EmlAdapter
from app.models import Inquiry
from app.store import Store


@dataclass
class Selection:
    new: list[tuple[Path, Inquiry]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (file, reason)
    waiting: list[Path] = field(default_factory=list)  # still being written; picked up next time
    unreadable: list[tuple[Path, str]] = field(default_factory=list)


def scan(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.eml")) if folder.is_dir() else []


def backfill_source_keys(store: Store, adapter: EmlAdapter) -> int:
    """Give inquiries created before source keys existed a key, so they are
    recognised as already processed. Uses the original file when it is still on
    disk, otherwise the stored Message-ID."""
    filled = 0
    for inquiry_id, inq in store.rows_missing_source_key():
        key = None
        if inq.source_path and Path(inq.source_path).exists():
            try:
                key = adapter.parse(Path(inq.source_path)).source_key
            except Exception:  # noqa: BLE001 - fall back to the stored id below
                key = None
        if key is None and inq.thread_id.startswith("<"):
            key = f"msgid:{inq.thread_id.strip().lower()}"
        if key:
            store.set_source_key(inquiry_id, key)
            filled += 1
    return filled


def select_new(paths: list[Path], store: Store, adapter: EmlAdapter, *, force: bool = False,
               min_age_seconds: float = 0.0, now: float | None = None) -> Selection:
    now = time.time() if now is None else now
    sel = Selection()
    seen_in_batch: dict[str, Path] = {}
    for path in paths:
        try:
            if min_age_seconds and now - path.stat().st_mtime < min_age_seconds:
                sel.waiting.append(path)
                continue
            inq = adapter.parse(path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            sel.unreadable.append((path, f"{type(exc).__name__}: {exc}"[:160]))
            continue
        key = inq.source_key
        if not force:
            existing = store.find_by_source_key(key)
            if existing:
                sel.skipped.append((path, f"already processed as {existing}"))
                continue
            if key in seen_in_batch:
                sel.skipped.append((path, f"same email as {seen_in_batch[key].name}"))
                continue
        if key:
            seen_in_batch[key] = path
        sel.new.append((path, inq))
    return sel
