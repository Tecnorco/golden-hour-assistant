"""Demo data for a hosted showcase (e.g. Streamlit Community Cloud).

The hosted app never processes emails (that needs the Gemini key, which should
not live on a public demo). Instead it starts from a committed snapshot: the 20
fictional sample emails, processed locally with `python -m app.cli demo-snapshot`.

Set GHA_DEMO_SNAPSHOT (env var or Streamlit secret) to the snapshot path, e.g.
"demo/assistant.db". When the app's database is missing or empty, the snapshot
is copied in; "Reset demo data" copies it again and clears the outbox.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


def _has_inquiries(db: Path) -> bool:
    if not db.exists() or db.stat().st_size == 0:
        return False
    try:
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def resolve_snapshot(value: str | None, root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    path = path if path.is_absolute() else root / path
    return path if path.exists() else None


def ensure_demo_db(db_path: Path, snapshot: Path | None) -> bool:
    """Copy the snapshot in if the app database is missing or empty. Returns True if copied."""
    if snapshot is None or _has_inquiries(db_path):
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, db_path)
    return True


def reset_demo(db_path: Path, snapshot: Path, outbox: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, db_path)
    if outbox.exists():
        for f in outbox.glob("*.eml"):
            f.unlink()
