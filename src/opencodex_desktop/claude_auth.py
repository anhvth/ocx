"""This machine's Claude login (the one `claude` uses), for claude-* requests from clients that
authenticate to the router with its token instead of their own login (Claude Desktop's gateway mode).

macOS keeps it in the login keychain ("Claude Code-credentials"); Linux in ~/.claude/.credentials.json.
The token is only ever sent to api.anthropic.com.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

KEYCHAIN_SERVICE = "Claude Code-credentials"


def _read() -> dict[str, Any] | None:
    try:
        if sys.platform == "darwin":
            raw = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                                 capture_output=True, text=True, timeout=10).stdout
        else:
            home = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
            raw = (home / ".credentials.json").read_text()
        return json.loads(raw).get("claudeAiOauth")
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def _refresh() -> None:
    """Let Claude Code refresh its own login (it rewrites the stored credential)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_OAUTH"))}
    try:
        subprocess.run(["claude", "--setting-sources", "", "auth", "status"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def access_token() -> str | None:
    cred = _read()
    if cred and cred.get("expiresAt", 0) < (time.time() + 120) * 1000:
        _refresh()
        cred = _read()
    if not cred or not cred.get("accessToken") or cred.get("expiresAt", 0) < time.time() * 1000:
        return None
    return cred["accessToken"]
