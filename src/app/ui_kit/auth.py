"""Login for the approval screen.

This is a demo gate (one shared account), not multi-user authentication.

- The default demo password is stored only as a salted PBKDF2-SHA256 hash, so it
  cannot be read from the source code.
- Set your own credentials without touching the code: GHA_UI_USERNAME and
  GHA_UI_PASSWORD as environment variables, or as Streamlit secrets (on
  Streamlit Community Cloud: App settings -> Secrets). When GHA_UI_PASSWORD is set,
  the default password stops working.
- Comparisons are constant-time, and a session is locked for a while after
  repeated failures.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable, MutableMapping
from typing import Any

DEFAULT_USERNAME = "admin"
_SALT = b"golden-hour-demo-v1"
_ITERATIONS = 200_000
# PBKDF2-SHA256 of the default demo password (shared with you separately).
DEFAULT_PASSWORD_HASH = "357ff696641a7c5c237569e12b34013f8b012723fde6f93b809c846557317c9a"

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


def hash_password(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _SALT, _ITERATIONS).hex()


def _setting(name: str, secrets: Callable[[str], Any] | None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if secrets is not None:
        try:
            found = secrets(name)
        except Exception:  # noqa: BLE001 - no secrets file configured is normal locally
            found = None
        if found:
            return str(found)
    return None


def check_credentials(username: str, password: str, secrets: Callable[[str], Any] | None = None) -> bool:
    expected_user = _setting("GHA_UI_USERNAME", secrets) or DEFAULT_USERNAME
    custom_password = _setting("GHA_UI_PASSWORD", secrets)
    user_ok = hmac.compare_digest(username.strip().encode(), expected_user.encode())
    if custom_password:
        pass_ok = hmac.compare_digest(password.encode(), custom_password.encode())
    else:
        pass_ok = hmac.compare_digest(hash_password(password), DEFAULT_PASSWORD_HASH)
    return user_ok and pass_ok


def try_login(state: MutableMapping[str, Any], username: str, password: str,
              secrets: Callable[[str], Any] | None = None, now: float | None = None) -> str | None:
    """Update session state. Returns an error message, or None on success."""
    now = time.time() if now is None else now
    locked_until = state.get("login_locked_until", 0.0)
    if now < locked_until:
        return f"Too many attempts. Try again in {int(locked_until - now) + 1} seconds."
    if check_credentials(username, password, secrets):
        state["authenticated"] = True
        state["user"] = username.strip()
        state["login_failures"] = 0
        return None
    failures = state.get("login_failures", 0) + 1
    state["login_failures"] = failures
    if failures >= MAX_FAILURES:
        state["login_locked_until"] = now + LOCKOUT_SECONDS
        state["login_failures"] = 0
        return f"Too many attempts. Try again in {LOCKOUT_SECONDS} seconds."
    return "Incorrect username or password."


def logout(state: MutableMapping[str, Any]) -> None:
    for key in ("authenticated", "user", "selected", "detail_pick"):
        state.pop(key, None)
    state["page"] = "inbox"
