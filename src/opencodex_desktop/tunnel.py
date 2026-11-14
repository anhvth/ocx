"""Public HTTPS address for the router, for Claude app SSH sessions (opt-in: `claude_app.ssh_hosts`).

In an SSH session the Claude app runs Claude Code on the remote host, which calls the gateway itself, and
the app refuses a loopback gateway for that. A Cloudflare quick tunnel (`cloudflared tunnel --url`) gives
the router an https://<random>.trycloudflare.com address reachable from anywhere. Requests arriving through
it may only use the Claude endpoints with the router token (see Router.handle); `off`/`stop` close it.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import STATE_DIR, Config

PIDFILE = STATE_DIR / "tunnel.pid"
LOGFILE = STATE_DIR / "tunnel.log"
URLFILE = STATE_DIR / "tunnel.url"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def cloudflared() -> str | None:
    return shutil.which("cloudflared") or next(
        (p for p in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared") if Path(p).exists()), None)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def current_url() -> str | None:
    pid = _pid()
    if pid and _alive(pid):
        try:
            return URLFILE.read_text().strip() or None
        except OSError:
            return None
    return None


def stop() -> bool:
    pid = _pid()
    for f in (PIDFILE, URLFILE):
        f.unlink(missing_ok=True)
    if pid and _alive(pid):
        os.kill(pid, signal.SIGTERM)
        return True
    return False


def start(cfg: Config) -> str:
    """Return the tunnel URL, reusing a running tunnel to this router (its URL stays the same)."""
    url = current_url()
    if url:
        return url
    stop()
    exe = cloudflared()
    if not exe:
        raise RuntimeError("cloudflared is not installed (macOS: `brew install cloudflared`)")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGFILE.write_text("")
    with open(LOGFILE, "ab") as log:
        proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", cfg.anthropic_base],
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    PIDFILE.write_text(str(proc.pid))
    for _ in range(300):
        if proc.poll() is not None:
            break
        m = URL_RE.search(LOGFILE.read_text(errors="replace"))
        if m and "Registered tunnel connection" in LOGFILE.read_text(errors="replace"):
            URLFILE.write_text(m.group(0))
            return m.group(0)
        time.sleep(0.1)
    stop()
    tail = LOGFILE.read_text(errors="replace").splitlines()[-10:]
    raise RuntimeError("cloudflared tunnel failed to start:\n" + "\n".join(tail))
