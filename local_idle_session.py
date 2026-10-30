"""Process-local, fail-closed Windows-authenticated session lease manager.

An opaque session token is persisted only in the current browser tab's sessionStorage.
No passwords or raw credentials are written to the browser store. Sessions expire
15 minutes after the latest authenticated activity, or on server restart.
"""
from __future__ import annotations
from datetime import datetime, timezone
import secrets
import threading
import time

TTL_SECONDS = 15 * 60
WARNING_SECONDS = 12 * 60
_LOCK = threading.RLock()
_LEASES: dict[str, dict] = {}


def create(actor: str) -> str:
    token = secrets.token_urlsafe(48)
    with _LOCK:
        _LEASES[token] = {"actor": actor, "last": time.monotonic()}
    return token


def check(token: str, *, touch: bool = False) -> tuple[str | None, bool]:
    """Return (actor, expired). Expired tokens are removed and never renewed."""
    if not isinstance(token, str) or len(token) < 40:
        return None, False
    with _LOCK:
        lease = _LEASES.get(token)
        if lease is None:
            return None, False
        if time.monotonic() - lease["last"] >= TTL_SECONDS:
            del _LEASES[token]
            return lease["actor"], True
        if touch:
            lease["last"] = time.monotonic()
        return lease["actor"], False


def remaining(token: str) -> float:
    with _LOCK:
        lease = _LEASES.get(token)
        if lease is None:
            return 0.0
        return max(0.0, TTL_SECONDS - (time.monotonic() - lease["last"]))


def invalidate(token: str) -> None:
    with _LOCK:
        _LEASES.pop(token, None)
