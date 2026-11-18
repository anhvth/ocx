"""ocx: one command to put custom models into Codex (Desktop app + CLI).

  ocx            same as `start`
  ocx start      discover models, write Codex config, (re)start the router
  ocx restart    same as `start`
  ocx off        stop the router and put Codex back to its defaults
  ocx status     what is running and which models Codex sees
  ocx init       write an example config to ~/.config/ocx/config.yaml
  ocx setup      guided walk-through: config, provider keys, login checks, start
  ocx update     install and activate the current published version
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import tomlkit

from . import __version__, ui
from .config import DEFAULT_CONFIG, LEGACY_CONFIG, SERVICE_IDS, STATE_DIR, Config, ConfigError, load
from .updates import latest_version, version_parts

PIDFILE = STATE_DIR / "router.pid"
LOGFILE = STATE_DIR / "router.log"
EXAMPLE = Path(__file__).with_name("example_config.yaml")
UPSTREAM_PACKAGE = "https://github.com/anhvth/ocx/archive/refs/heads/main.tar.gz"


def _check_update() -> None:
    latest = latest_version()
    if latest and version_parts(latest) > version_parts(__version__):
        ui.warn(f"ocx {latest} is available (installed: {__version__}); run `ocx update`")


def cmd_update(config_path: str | None) -> int:
    latest = latest_version()
    if latest is None:
        ui.err("could not read the published OCX version; check your connection to GitHub")
        return 1
    if version_parts(latest) < version_parts(__version__):
        ui.info(f"installed ocx {__version__} is newer than the published {latest}")
        return 0
    try:
        subprocess.run(["uv", "tool", "install", "--force", "--refresh", UPSTREAM_PACKAGE], check=True)
        bin_dir = subprocess.check_output(["uv", "tool", "dir", "--bin"], text=True).strip()
        cmd = [str(Path(bin_dir) / "ocx")]
        if config_path:
            cmd += ["--config", config_path]
        ui.ok("OCX installed; starting the updated router")
        return subprocess.run([*cmd, "start"]).returncode
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except OSError as exc:
        ui.err(f"could not update OCX: {exc}")
        return 1


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
    if h is not None and h.get("service") not in SERVICE_IDS:
        return False
    pid = h.get("pid") if h else None
    pid = pid or _read_pid()
    if type(pid) is not int or pid <= 1 or not _pid_alive(pid):
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
    cmd = [sys.executable, "-m", "ocx"]
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
        if h and h.get("service") in SERVICE_IDS and h.get("pid") == proc.pid:
            return proc.pid
        time.sleep(0.1)
    tail = LOGFILE.read_text(errors="replace").splitlines()[-15:]
    raise RuntimeError("router failed to start:\n" + "\n".join(tail))


def _restart_stale_codex(cfg: Config, catalog: Path) -> None:
    sock = cfg.codex_home / "app-server-control" / "app-server-control.sock"
    try:
        socket_stat = sock.stat()
        if not stat.S_ISSOCK(socket_stat.st_mode) or catalog.stat().st_mtime_ns <= socket_stat.st_mtime_ns:
            return
    except OSError:
        return

    cmd = ["codex", "app-server", "daemon"]
    env = {**os.environ, "CODEX_HOME": str(cfg.codex_home)}
    manual = "Run `codex app-server daemon restart` when ready."
    try:
        status = subprocess.run([*cmd, "version"], env=env, capture_output=True, text=True, timeout=3)
        info = json.loads(status.stdout) if status.returncode == 0 else None
        if not isinstance(info, dict) or info.get("status") != "running":
            return
        if "managedCodexVersion" in info and info["managedCodexVersion"] is None:
            ui.warn("The running app-server was launched outside the Codex daemon. "
                    "Reconnect the Codex SSH session or relaunch the client that owns it to reload the catalog.")
            return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        ui.warn(f"Could not check the Codex daemon. {manual}")
        return

    ui.warn("Codex's running app-server predates the model catalog; restarting it (open CLI sessions reconnect).")
    try:
        result = subprocess.run([*cmd, "restart"], env=env, timeout=60)
        if result.returncode == 0:
            ui.ok("Codex app-server restarted")
            return
    except (OSError, subprocess.TimeoutExpired) as e:
        ui.warn(str(e))
    ui.warn(f"Could not restart the Codex daemon. {manual}")


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    from .claude_setup import apply_claude_config
    from .codex_setup import apply_codex_config, build_catalog, write_catalog
    from .discovery import discover, save_routes

    ui.head(f"ocx {__version__}")
    ui.info(f"config: {cfg.path or f'(none; {DEFAULT_CONFIG} not found -> ChatGPT models only; run `ocx init`)'}")
    h = _health(cfg)
    if h and h.get("service") not in SERVICE_IDS:
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

    from .pi_setup import apply_pi_config
    ui.head("Pi")
    apply_pi_config(cfg, d)

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
    _restart_stale_codex(cfg, catalog)
    ui.info("If the Codex Desktop app still shows the old list, quit and reopen it.")
    ui.info("Claude Code (`claude`, `cl`) sees the new models in /model right away.")
    ui.info("Pi: restart to refresh the model list. In /model, Enter switches for this session; Ctrl+S saves the default.")
    ui.info("Turn it off any time with: ocx off")
    return 0


def cmd_off(cfg: Config, args: argparse.Namespace) -> int:
    from .claude_setup import restore_claude_config
    from .codex_setup import restore_codex_config
    ui.head("ocx off")
    _stop_router(cfg)
    restore_codex_config(cfg)
    restore_claude_config(cfg)
    from .pi_setup import restore_pi_config
    restore_pi_config(cfg)
    return 0


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    from . import tunnel
    _stop_router(cfg)
    if tunnel.stop():
        ui.ok("closed the Claude app tunnel")
    ui.warn("Codex config still points at the router; run `ocx off` to go back to defaults")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    from .discovery import _openai_models, load_routes, read_openai_auth
    ui.head(f"ocx {__version__}")
    ui.info(f"config: {cfg.path or '(none)'}   codex home: {cfg.codex_home}")
    auth = read_openai_auth(cfg.codex_home)
    (ui.ok if auth.mode != "none" else ui.warn)(f"Codex login: {auth.mode}" + (f" ({auth.email})" if auth.email else ""))
    h = _health(cfg)
    if h and h.get("service") in SERVICE_IDS:
        ui.ok(f"router up on {cfg.base_url} (pid {h.get('pid')})")
    elif h:
        ui.err(f"port {cfg.listen_port} is used by another program")
    else:
        ui.warn("router is not running (run `ocx start`)")
    path = cfg.codex_home / "config.toml"
    doc = tomlkit.parse(path.read_text()) if path.exists() else {}
    if doc.get("model_provider") in (cfg.provider_id, *SERVICE_IDS):
        ui.ok(f"Codex uses the router (model: {doc.get('model', '-')})")
    else:
        ui.info(f"Codex is on its default provider ({doc.get('model_provider', 'openai')}); router is off for Codex")
    if cfg.openai_enabled and auth.mode != "none":
        unfiltered = replace(cfg, openai_include=[], openai_exclude_regex=None)
        available = {m["slug"] for m in _openai_models(unfiltered, auth)}
        included = available & set(cfg.openai_include) if cfg.openai_include else available
        pattern = cfg.openai_exclude_regex
        excluded = {slug for slug in included if pattern and pattern.search(slug)}
        listed = set()
        if doc.get("model_catalog_json"):
            try:
                catalog = json.loads(Path(doc["model_catalog_json"]).read_text())
                listed = {m["slug"] for m in catalog["models"]}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        ui.info("GPT in configured catalog: " + (", ".join(sorted(s for s in listed if "/" not in s)) or "(none)"))
        if available - included:
            ui.info("GPT excluded by openai.include: " + ", ".join(sorted(available - included)))
        if excluded:
            ui.info("GPT excluded by openai.exclude_regex: " + ", ".join(sorted(excluded)))
        if missing := included - excluded - listed:
            ui.warn("GPT available but missing from catalog: " + ", ".join(sorted(missing))
                    + " (run `ocx start`)")
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
    if not args.config and not args.force and not dst.exists() and LEGACY_CONFIG.exists():
        ui.warn(f"existing config at {LEGACY_CONFIG}; run `ocx start` to use it")
        return 1
    if dst.exists() and not args.force:
        ui.warn(f"{dst} already exists (use --force to overwrite)")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(EXAMPLE.read_text())
    ui.ok(f"wrote {dst}; edit it, then run `ocx start`")
    return 0


def _ask_key_file(path: Path, prompt: str) -> None:
    """Ask for an API key with hidden input and save it 0600. Empty input keeps the existing file."""
    if path.exists():
        ui.ok(f"key file exists: {path} (Enter keeps it)")
    key = getpass.getpass(prompt).strip()
    if not key:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n")
    path.chmod(0o600)
    ui.ok(f"saved {path} (…{key[-4:]})")


def cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    ui.head(f"ocx setup {__version__}")
    if not sys.stdin.isatty():
        ui.err("setup is interactive; run it in a terminal (or use `ocx init` + edit the YAML)")
        return 1

    ui.head("1/4 Config")
    path = cfg.path
    if path is None:
        path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE.read_text())
        ui.ok(f"wrote {path} from the example config")
    else:
        ui.ok(f"using {path}")

    ui.head("2/4 Codex login (ChatGPT models)")
    from .discovery import http_json, read_openai_auth
    if read_openai_auth(cfg.codex_home).mode == "none":
        ui.warn(f"not logged in to Codex ({cfg.codex_home}); run `codex login` or sign in in the Codex app")
    else:
        ui.ok("Codex login found; GPT models come from it")

    ui.head("3/4 Provider keys")
    import yaml
    from . import config as config_mod
    doc = yaml.safe_load(path.read_text()) or {}
    # Read the YAML directly: load() raises on a missing key file, which setup is here to fix.
    raw_providers = dict(doc.get("providers") or {})
    if doc.get("anthropic"):
        raw_providers["anthropic"] = doc["anthropic"]
    for name, raw in raw_providers.items():
        raw = raw or {}
        base_url = str(raw.get("base_url") or (config_mod.ANTHROPIC_BASE if name == "anthropic" else ""))
        if raw.get("enabled") is False:
            ui.info(f"{name}: disabled in config; skipped")
            continue
        if raw.get("api_key_file"):
            key_path = Path(os.path.expandvars(str(raw["api_key_file"]))).expanduser()
            if key_path.exists():
                ui.ok(f"{name}: key file {key_path}")
            else:
                _ask_key_file(key_path, f"    {name} API key (hidden, Enter to skip): ")
        elif raw.get("api_key") or raw.get("api_key_env") or "localhost" in base_url or "127.0.0.1" in base_url:
            ui.ok(f"{name}: key configured")
        else:
            ui.warn(f"{name}: no key configured; add api_key_file/api_key_env under it in {path}")
        if not base_url:
            ui.warn(f"{name}: no base_url in {path}; skipped")
            continue
        key = None
        if raw.get("api_key_file"):
            key_path = Path(os.path.expandvars(str(raw["api_key_file"]))).expanduser()
            if key_path.exists():
                key = key_path.read_text().strip()
        elif raw.get("api_key"):
            key = str(raw["api_key"]).strip()
        elif raw.get("api_key_env"):
            key = os.environ.get(str(raw["api_key_env"]))
        headers = dict(raw.get("headers") or {})
        if key:
            if name == "anthropic":
                headers.setdefault("x-api-key", key)
                headers.setdefault("anthropic-version", "2023-06-01")
            else:
                headers["authorization"] = f"Bearer {key}"
        try:
            code, _ = http_json("GET", f"{base_url}/models", headers=headers, timeout=10)
            if code == 200:
                ui.ok(f"{name}: reachable, key accepted")
            else:
                ui.warn(f"{name}: /models HTTP {code}" + (" (wrong key or plan?)" if code in (401, 403) else ""))
        except OSError as e:
            ui.warn(f"{name}: {getattr(e, 'reason', e)}")

    ui.head("4/4 Start")
    answer = input("    Run `ocx start` now? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        return cmd_start(load(str(path)), args)
    ui.info(f"Run `ocx start` when ready. Edit {path} any time, then start again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ocx", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", "-c", help=f"YAML config (default {DEFAULT_CONFIG})")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("start", aliases=["restart"], help="discover + configure Codex + (re)start router")
    for name, h in [("off", "stop router and restore Codex defaults"),
                    ("stop", "stop the router only"), ("status", "show state"),
                    ("serve", "run the router in the foreground"),
                    ("setup", "guided walk-through: config, keys, login checks, start"),
                    ("update", "install and activate the current published version")]:
        sub.add_parser(name, help=h)
    p = sub.add_parser("init", help="write an example config")
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    cmd = "start" if args.cmd in (None, "restart") else args.cmd
    if cmd == "update":
        return cmd_update(args.config)
    if cmd != "serve":  # The background router is launched by the CLI.
        _check_update()
    if cmd == "init":
        return cmd_init(None, args)  # type: ignore[arg-type]
    try:
        cfg = load(args.config)
    except ConfigError as e:
        ui.err(f"config error: {e}")
        return 2
    return {"start": cmd_start, "off": cmd_off, "stop": cmd_stop, "status": cmd_status,
            "serve": cmd_serve, "setup": cmd_setup}[cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
