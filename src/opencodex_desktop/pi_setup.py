"""Keep Pi's scoped model picker on the same discovery results as Codex."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from . import ui
from .config import TOKEN_FILE, Config, ConfigError, router_token
from .discovery import Discovery

PROVIDER = "opencodex-desktop"
SETTINGS_KEYS = ("enabledModels", "defaultProvider", "defaultModel")


def agent_home() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    return data


def _write(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2) + "\n"
    if path.exists() and path.read_text() == text:
        return
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def build_models(d: Discovery) -> list[dict]:
    models = []
    for m in d.openai_models:
        efforts = [e["effort"] for e in m.get("supported_reasoning_levels", [])]
        entry = _model(m["slug"], m.get("context_window", 128_000),
                       m.get("max_output_tokens"), m.get("input_modalities", ["text"]), efforts)
        if d.openai.mode == "chatgpt":
            entry["compat"] = {"supportsMaxOutputTokens": False}
        models.append(entry)
    for r in d.routes:
        models.append(_model(r.slug, r.context_window, r.max_output_tokens, r.input_modalities, r.efforts))
    return models


def _model(slug: str, context: int, output: int | None, modalities: list[str], efforts: list[str]) -> dict:
    return {"id": slug, "name": slug, "contextWindow": context,
            # shortcut: unknown output limits use Pi's conservative default until discovery reports one.
            "maxTokens": min(output or 16384, context),
            "input": [x for x in modalities if x in ("text", "image")] or ["text"],
            "reasoning": any(e not in ("none", "off") for e in efforts),
            "thinkingLevelMap": {level: ("none" if level == "off" else level)
                                 if ("none" if level == "off" else level) in efforts else None
                                 for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max")}}


def apply_pi_config(cfg: Config, d: Discovery) -> None:
    home = agent_home()
    if not home.is_dir():
        return  # Do not create Pi configuration on machines that do not use Pi.
    models_path, settings_path = home / "models.json", home / "settings.json"
    models, settings = _read(models_path), _read(settings_path)
    providers = models.setdefault("providers", {})
    backup = home / "opencodex-desktop-backup.json"
    if not backup.exists():
        _write(backup, {"provider": providers.get(PROVIDER),
                        "settings": {k: settings[k] for k in SETTINGS_KEYS if k in settings}})
    router_token()
    rows = build_models(d)
    providers[PROVIDER] = {"name": "opencodex-desktop", "baseUrl": cfg.base_url,
                           "api": "openai-responses", "apiKey": f"!cat {shlex.quote(str(TOKEN_FILE))}",
                           "models": rows}
    settings["enabledModels"] = [f"{PROVIDER}/*"]
    slugs = [m["id"] for m in rows]
    current = settings.get("defaultModel")
    if current not in slugs:
        current = cfg.default_model if cfg.default_model in slugs else next(iter(slugs), None)
    settings["defaultProvider"] = PROVIDER
    if current:
        settings["defaultModel"] = current
    _write(models_path, models)
    _write(settings_path, settings)
    ui.ok(f"updated {models_path} ({len(rows)} models; /model defaults to this project's list)")


def restore_pi_config(cfg: Config) -> None:
    home = agent_home()
    backup = home / "opencodex-desktop-backup.json"
    if not backup.exists():
        return
    saved = _read(backup)
    models_path, settings_path = home / "models.json", home / "settings.json"
    models, settings = _read(models_path), _read(settings_path)
    providers = models.get("providers", {})
    if saved["provider"] is None:
        providers.pop(PROVIDER, None)
    else:
        providers[PROVIDER] = saved["provider"]
    for key in SETTINGS_KEYS:
        settings.pop(key, None)
    settings.update(saved["settings"])
    _write(models_path, models)
    _write(settings_path, settings)
    backup.unlink()
    ui.ok(f"restored Pi's previous model scope and defaults in {home}")
