"""Point Claude Code (CLI, `cl`) and the Claude desktop app at the router.

Claude Code: ~/.claude/settings.json gets
  env.ANTHROPIC_BASE_URL       the router (Claude Code appends /v1/messages)
  env.ANTHROPIC_CUSTOM_HEADERS x-opencodex-token: <router token>   (claude-* still use your own login)
  modelPicker.options          one row per GPT / <provider>/<model> model, after the built-in Claude rows
  modelSettings.<model>        autoCompactWindow below the model's real context window
Claude desktop app (macOS, third-party mode): a gateway profile in
  ~/Library/Application Support/Claude-3p/configLibrary, made the applied profile.
`restore_claude_config` removes exactly what was added.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import ui
from .config import Config, router_token
from .discovery import Discovery

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
SETTINGS = CLAUDE_HOME / "settings.json"
TAG = "via opencodex-desktop"
ONE_M = 900_000  # windows at least this big get Claude Code's 1M ("[1m]") window
DESKTOP = Path("~/Library/Application Support/Claude-3p").expanduser()
PROFILE_ID = "00000000-0000-4000-8000-00000000c0de"
PROFILE_NAME = "opencodex-desktop"
CLAUDE_ROWS = [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
               ("claude-haiku-5-5", "Claude Haiku 5.5"), ("claude-fable-5-1", "Claude Fable 5.1")]


def _models(cfg: Config, d: Discovery) -> list[dict[str, Any]]:
    """Every non-Claude model a Claude client can use: GPT (from the Codex login), then routed
    models whose provider speaks the Anthropic Messages API (`anthropic_url`)."""
    claude_ok = {p.name for p in cfg.providers if p.anthropic_url}
    skipped = sorted({r.provider for r in d.routes if r.provider not in claude_ok})
    if skipped:
        ui.info(f"not in Claude (no `anthropic_url:`): {', '.join(skipped)}")
    rows = [{"slug": m["slug"], "label": m.get("display_name") or m["slug"], "ctx": m.get("context_window") or 0}
            for m in d.openai_models]
    rows += [{"slug": r.slug, "label": r.display_name or r.slug, "ctx": r.context_window} for r in d.routes if r.provider in claude_ok]
    return rows


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    """Atomic, owner-only (the file carries the router token). Replaces a symlink with a real file."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.bak-opencodex-desktop-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst, follow_symlinks=True)
    os.chmod(dst, 0o600)
    return dst


def _strip_ours(s: dict[str, Any]) -> None:
    env = s.get("env") or {}
    if "x-opencodex-token" in str(env.get("ANTHROPIC_CUSTOM_HEADERS", "")):
        env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
        env.pop("ANTHROPIC_BASE_URL", None)
    if not env:
        s.pop("env", None)
    picker = s.get("modelPicker") or {}
    opts = [o for o in picker.get("options") or [] if TAG not in str(o.get("description", ""))]
    if opts:
        picker["options"] = opts
    else:
        picker.pop("options", None)
    if not picker:
        s.pop("modelPicker", None)
    ms = s.get("modelSettings") or {}
    for k in [k for k, v in ms.items() if isinstance(v, dict) and v.get("_opencodexDesktop")]:
        del ms[k]
    if "modelSettings" in s and not ms:
        del s["modelSettings"]


def apply_claude_config(cfg: Config, d: Discovery) -> None:
    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    s = _read(SETTINGS)
    before = json.dumps(s, sort_keys=True)
    _strip_ours(s)
    env = s.setdefault("env", {})
    for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if env.get(k):
            ui.warn(f"{SETTINGS}: env.{k} is set, so Claude models use that key instead of your Claude login")
    env["ANTHROPIC_BASE_URL"] = cfg.anthropic_base
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-opencodex-token: {router_token()}"
    picker = s.setdefault("modelPicker", {})
    opts = picker.setdefault("options", [])
    ms = s.setdefault("modelSettings", {})
    rows = _models(cfg, d)
    for r in rows:
        big = r["ctx"] >= ONE_M
        opts.append({"model": r["slug"] + ("[1m]" if big else ""), "label": r["label"],
                     "description": f"{ui.fmt_tokens(r['ctx'])} context, {TAG}" if r["ctx"] else TAG})
        if r["ctx"]:
            ms[r["slug"]] = {"autoCompactWindow": int(r["ctx"] * 0.9), "_opencodexDesktop": True}
    if not ms:
        del s["modelSettings"]
    small = [r["slug"] for r in rows if r["ctx"] and r["ctx"] < 200_000]
    if small:
        ui.warn(f"Claude Code assumes a 200k window for {', '.join(small)}; long sessions may overflow them")
    if json.dumps(s, sort_keys=True) != before or SETTINGS.is_symlink():
        b = _backup(SETTINGS)
        _write(SETTINGS, s)
        ui.ok(f"updated {SETTINGS} ({len(rows)} models in /model" + (f"; backup: {b.name}" if b else "") + ")")
    else:
        ui.ok(f"{SETTINGS} already up to date")
    apply_desktop_config(cfg, rows)


def restore_claude_config(cfg: Config) -> None:
    s = _read(SETTINGS)
    before = json.dumps(s, sort_keys=True)
    _strip_ours(s)
    if json.dumps(s, sort_keys=True) != before:
        _backup(SETTINGS)
        _write(SETTINGS, s)
        ui.ok(f"removed the router from {SETTINGS} (Claude Code back to its defaults)")
    restore_desktop_config()


# ---------------------------------------------------------------- Claude desktop app (macOS)
def _desktop_running() -> bool:
    return sys.platform == "darwin" and subprocess.run(["pgrep", "-x", "Claude"], stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL).returncode == 0


def apply_desktop_config(cfg: Config, rows: list[dict[str, Any]]) -> None:
    if sys.platform != "darwin" or not DESKTOP.exists():
        return
    lib = DESKTOP / "configLibrary"
    lib.mkdir(exist_ok=True)
    models: list[dict[str, Any]] = [{"name": m, "labelOverride": label} for m, label in CLAUDE_ROWS]
    for r in rows:
        entry: dict[str, Any] = {"name": r["slug"], "labelOverride": r["label"]}
        if r["ctx"] >= ONE_M:
            entry["supports1m"] = True
        models.append(entry)
    path = lib / f"{PROFILE_ID}.json"
    profile = _read(path)
    profile.update({
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": cfg.anthropic_base,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceGatewayApiKey": router_token(),
        "inferenceModels": models,
        "modelDiscoveryEnabled": False,
    })
    _write(path, profile)
    meta_path = lib / "_meta.json"
    meta = _read(meta_path)
    entries = [e for e in meta.get("entries") or [] if e.get("id") != PROFILE_ID]
    meta["entries"] = entries + [{"id": PROFILE_ID, "name": PROFILE_NAME}]
    if meta.get("appliedId") != PROFILE_ID:
        meta["previousAppliedId"] = meta.get("appliedId")
    meta["appliedId"] = PROFILE_ID
    _write(meta_path, meta)
    main = DESKTOP / "claude_desktop_config.json"
    mc = _read(main)
    if mc.get("deploymentMode") != "3p":
        mc["deploymentMode"] = "3p"
        _write(main, mc)
    ui.ok(f"Claude desktop app: gateway profile '{PROFILE_NAME}' ({len(models)} models)"
          + ("; quit and reopen Claude to load it" if _desktop_running() else ""))


def restore_desktop_config() -> None:
    lib = DESKTOP / "configLibrary"
    meta_path = lib / "_meta.json"
    if sys.platform != "darwin" or not meta_path.exists():
        return
    meta = _read(meta_path)
    if not any(e.get("id") == PROFILE_ID for e in meta.get("entries") or []):
        return
    meta["entries"] = [e for e in meta["entries"] if e.get("id") != PROFILE_ID]
    if meta.get("appliedId") == PROFILE_ID:
        prev = meta.pop("previousAppliedId", None)
        if prev and (lib / f"{prev}.json").exists():
            meta["appliedId"] = prev
        else:
            meta.pop("appliedId", None)
    _write(meta_path, meta)
    (lib / f"{PROFILE_ID}.json").unlink(missing_ok=True)
    ui.ok("removed the opencodex-desktop profile from the Claude desktop app")
