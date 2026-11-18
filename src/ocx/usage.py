"""Fetch the signed-in Codex account's usage/credits for the public landing page.

Only non-secret aggregates are returned (plan, window percentages, credit balance,
reset count). Identifiers such as email / user_id / account_id are never exposed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_TTL = 60.0


def _window(w: Any) -> dict | None:
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    try:
        secs = int(w.get("limit_window_seconds") or 0)
        left = max(0, min(100, 100 - int(w["used_percent"])))
    except (TypeError, ValueError):
        return None
    label = "5-hour limit" if secs <= 18000 else ("Weekly limit" if secs >= 604800 else "Limit")
    return {"label": label, "left": left, "reset_at": w.get("reset_at"), "seconds": secs}


def _sanitize(d: Any) -> dict | None:
    if not isinstance(d, dict):
        return None
    rl = d.get("rate_limit") or {}
    windows = [w for w in (_window(rl.get("primary_window")), _window(rl.get("secondary_window"))) if w]
    credits = d.get("credits") or {}
    balance = None
    if credits.get("unlimited"):
        balance = "Unlimited"
    elif credits.get("has_credits"):
        try:
            n = round(float(credits.get("balance", 0)))
            balance = f"{n:,} credits"
        except (TypeError, ValueError):
            balance = None
    resets = (d.get("rate_limit_reset_credits") or {}).get("available_count") or 0
    return {"plan": str(d.get("plan_type") or "").strip(), "windows": windows,
            "credits": balance, "resets": int(resets)}


def fetch_codex_usage(auth: Any, timeout: float = 6.0) -> dict | None:
    """Return sanitized usage, or None when not signed in / unreachable. Cached ~60s."""
    now = time.monotonic()
    if now - _CACHE["at"] < _TTL:
        return _CACHE["value"]
    value = None
    try:
        token = getattr(auth, "access_token", None) or getattr(auth, "api_key", None)
        if getattr(auth, "mode", None) == "chatgpt" and getattr(auth, "upstream", None) and token:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                       "User-Agent": "codex_cli_rs/0.100.0 (Mac OS 14.0.0; arm64)",
                       "originator": "codex_cli_rs"}
            if getattr(auth, "account_id", None):
                headers["chatgpt-account-id"] = auth.account_id
            req = urllib.request.Request(auth.upstream + "/usage", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                value = _sanitize(json.load(r))
    except (OSError, ValueError, urllib.error.URLError, KeyError, TypeError):
        value = None
    _CACHE["at"] = now
    _CACHE["value"] = value
    return value
