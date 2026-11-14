"""opencodex-desktop: one command to put custom models into Codex (Desktop app + CLI).

  opencodex-desktop            same as `start`
  opencodex-desktop start      discover models, write Codex config, (re)start the router
  opencodex-desktop off        stop the router and put Codex back to its defaults
  opencodex-desktop status     what is running and which models Codex sees
  opencodex-desktop init       write an example config to ~/.config/opencodex-desktop/config.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, ui
from .config import DEFAULT_CONFIG, STATE_DIR, Config, ConfigError, load

PIDFILE = STATE_DIR / "router.pid"
LOGFILE = STATE_DIR / "router.log"
EXAMPLE = Path(__file__).with_name("example_config.yaml")


def _health(cfg: Config, timeout: float = 2) -> dict | None:
    from .discovery import http_json
    try:
        status, data = http_json("GET", f"http://{cfg.listen_host}:{cfg.listen_port}/healthz", timeout=timeout)
    except OSError:
        return None
    return data if status == 200 and isinstance(data, dict) else {"service": "unknown"}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _read_pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _stop_router(cfg: Config, quiet: bool = False) -> bool:
    h = _health(cfg)
    pid = (h or {}).get("pid") if (h or {}).get("service") == "opencodex-desktop" else None
    pid = pid or _read_pid()
    if not pid or not _pid_alive(pid):
        PIDFILE.unlink(missing_ok=True)
        if not quiet:
            ui.ok("router is not running")
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    PIDFILE.unlink(missing_ok=True)
    if not quiet:
        ui.ok(f"stopped router (pid {pid})")
    return True


def _spawn_router(cfg: Config) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "opencodex_desktop"]
    if cfg.path:
        cmd += ["--config", str(cfg.path)]
    cmd.append("serve")
    env = {**os.environ, "CODEX_HOME": str(cfg.codex_home)}
    with open(LOGFILE, "ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=env)
    PIDFILE.write_text(str(proc.pid))
    for _ in range(100):
        if proc.poll() is not None:
            break
        h = _health(cfg, timeout=1)
        if h and h.get("service") == "opencodex-desktop":
            return proc.pid
        time.sleep(0.1)
    tail = LOGFILE.read_text(errors="replace").splitlines()[-15:]
    raise RuntimeError("router failed to start:\n" + "\n".join(tail))


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    from .claude_setup import apply_claude_config
    from .codex_setup import apply_codex_config, build_catalog, write_catalog
    from .discovery import discover, save_routes

    ui.head(f"opencodex-desktop {__version__}")
    ui.info(f"config: {cfg.path or f'(none; {DEFAULT_CONFIG} not found -> ChatGPT models only; run `opencodex-desktop init`)'}")
    h = _health(cfg)
    if h and h.get("service") != "opencodex-desktop":
        ui.err(f"port {cfg.listen_port} is used by another program; set `listen:` in the config to a free port")
        return 1

    d = discover(cfg)
    if not d.openai_models and not d.routes:
        ui.err("no usable models at all (not logged in to Codex and no reachable provider); nothing to do")
        return 1

    ui.head("Codex")
    save_routes(d.routes)
    catalog = write_catalog(build_catalog(d))
    ui.ok(f"model catalog: {catalog} ({len(d.openai_models)} ChatGPT + {len(d.routes)} custom)")
    apply_codex_config(cfg, d, catalog)

    ui.head("Claude")
    apply_claude_config(cfg, d)

    ui.head("Router")
    if _stop_router(cfg, quiet=True):
        ui.info("stopped the previous router")
    try:
        pid = _spawn_router(cfg)
    except RuntimeError as e:
        ui.err(str(e))
        return 1
    ui.ok(f"listening on {cfg.base_url} (pid {pid}, log {LOGFILE})")

    ui.head("Done")
    if d.errors:
        ui.warn(f"{d.errors} provider(s) unavailable (see ⚠ above); their models are not listed in Codex")
    ui.info("Restart the Codex app to pick up the model list. The codex CLI attaches to a background")
    ui.info("daemon that keeps its old list: `codex app-server daemon restart` (closes open codex sessions).")
    ui.info("Claude Code (`claude`, `cl`) sees the new models in /model right away.")
    ui.info("Turn it off any time with: opencodex-desktop off")
    return 0


def cmd_off(cfg: Config, args: argparse.Namespace) -> int:
    from .claude_setup import restore_claude_config
    from .codex_setup import restore_codex_config
    from .discovery import load_routes
    ui.head("opencodex-desktop off")
    _stop_router(cfg)
    restore_codex_config(cfg, {r.slug for r in load_routes()})
    restore_claude_config(cfg)
    return 0


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    _stop_router(cfg)
    ui.warn("Codex config still points at the router; run `opencodex-desktop off` to go back to defaults")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    import tomlkit
    from .discovery import load_routes, read_openai_auth
    ui.head(f"opencodex-desktop {__version__}")
    ui.info(f"config: {cfg.path or '(none)'}   codex home: {cfg.codex_home}")
    auth = read_openai_auth(cfg.codex_home)
    (ui.ok if auth.mode != "none" else ui.warn)(f"Codex login: {auth.mode}" + (f" ({auth.email})" if auth.email else ""))
    h = _health(cfg)
    if h and h.get("service") == "opencodex-desktop":
        ui.ok(f"router up on {cfg.base_url} (pid {h.get('pid')})")
    elif h:
        ui.err(f"port {cfg.listen_port} is used by another program")
    else:
        ui.warn("router is not running (run `opencodex-desktop start`)")
    path = cfg.codex_home / "config.toml"
    doc = tomlkit.parse(path.read_text()) if path.exists() else {}
    if doc.get("model_provider") == cfg.provider_id:
        ui.ok(f"Codex uses the router (model: {doc.get('model', '-')})")
    else:
        ui.info(f"Codex is on its default provider ({doc.get('model_provider', 'openai')}); router is off for Codex")
    from .discovery import http_json, provider_headers
    routes = load_routes()
    for p in cfg.providers:
        if not p.enabled:
            continue
        try:
            code, _ = http_json("GET", f"{p.base_url}/models", headers=provider_headers(p), timeout=5)
            up = code == 200
            why = f"HTTP {code}"
        except OSError as e:
            up, why = False, str(getattr(e, "reason", e))
        mine = [r for r in routes if r.provider == p.name]
        if up:
            ui.ok(f"{p.name}: reachable, {len(mine)} model(s) listed in Codex")
        else:
            ui.warn(f"{p.name}: NOT reachable ({why}); its models are not listed"
                    + (" (the router drops them within a minute)" if mine else ""))
        for r in mine:
            ui.info(f"{r.slug:50s} ctx {ui.fmt_tokens(r.context_window):>5s} ({r.context_source}) wire {r.wire}")
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .router import serve
    serve(cfg)
    return 0


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    dst = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG
    if dst.exists() and not args.force:
        ui.warn(f"{dst} already exists (use --force to overwrite)")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(EXAMPLE.read_text())
    ui.ok(f"wrote {dst}; edit it, then run `opencodex-desktop start`")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="opencodex-desktop", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", "-c", help=f"YAML config (default {DEFAULT_CONFIG})")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    for name, h in [("start", "discover + configure Codex + (re)start router"),
                    ("off", "stop router and restore Codex defaults"),
                    ("stop", "stop the router only"), ("status", "show state"),
                    ("serve", "run the router in the foreground")]:
        sub.add_parser(name, help=h)
    p = sub.add_parser("init", help="write an example config")
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    cmd = args.cmd or "start"
    if cmd == "init":
        return cmd_init(None, args)  # type: ignore[arg-type]
    try:
        cfg = load(args.config)
    except ConfigError as e:
        ui.err(f"config error: {e}")
        return 2
    return {"start": cmd_start, "off": cmd_off, "stop": cmd_stop, "status": cmd_status,
            "serve": cmd_serve}[cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
