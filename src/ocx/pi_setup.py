"""Register the same discovered models in Pi without setting its startup model or scope."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from . import ui
from .config import DEFAULT_PROVIDER_ID, LEGACY_PROVIDER_ID, TOKEN_FILE, Config, ConfigError, router_token
from .discovery import Discovery
from .local_settings import managed_local_link

PROVIDER = DEFAULT_PROVIDER_ID


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
    # Preserve managed links while keeping generated settings at their local target.
    if managed_local_link(path):
        path = path.resolve()
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


def _restore_legacy_scope(home: Path, providers: dict, backups: dict) -> None:
    if not any("settings" in saved for saved in backups.values()):
        return
    path = home / "settings.json"
    settings = _read(path)
    if "enabledModels" not in settings:
        return
    for provider in backups:
        rows = providers.get(provider, {}).get("models", [])
        # Only undo a scope that still matches the old generated list.
        if rows and settings["enabledModels"] == [f"{provider}/{row['id']}" for row in rows]:
            original = next(reversed(backups.values())).get("settings", {})  # Oldest backup is last.
            settings.pop("enabledModels")
            if "enabledModels" in original:
                settings["enabledModels"] = original["enabledModels"]
            _write(path, settings)
            return


def _drop_stale_scope(home: Path, rows: list[dict]) -> None:
    """Drop an OCX-generated picker scope that outlived its models.

    Pi treats a non-empty `enabledModels` as an allowlist, so a scope written from an older
    discovery hides every model added since. Only an OCX-only scope naming a model that no longer
    exists is removed, which puts every discovered model back in the picker; a scope mixing other
    providers, or naming only live models, is the user's choice and is left alone.
    """
    path = home / "settings.json"
    settings = _read(path)
    scope = settings.get("enabledModels")
    prefixes = (f"{PROVIDER}/", f"{LEGACY_PROVIDER_ID}/")
    if not isinstance(scope, list) or not scope:
        return
    # A glob or a `:<level>` suffix is a hand-written pattern; leave the whole scope alone.
    if not all(isinstance(e, str) and e.startswith(prefixes) and not any(c in e for c in "*?[{:")
               for e in scope):
        return
    ids = {row["id"] for row in rows}
    ours = {e.split("/", 1)[1] for e in scope}
    if ours <= ids:
        return
    settings.pop("enabledModels")
    _write(path, settings)


def apply_pi_config(cfg: Config, d: Discovery) -> None:
    home = agent_home()
    if not home.is_dir():
        return  # Do not create Pi configuration on machines that do not use Pi.
    models_path = home / "models.json"
    models = _read(models_path)
    providers = models.setdefault("providers", {})
    router_token()
    backups = _read_backups(home)
    _restore_legacy_scope(home, providers, backups)
    for provider, saved in backups.items():
        if "settings" in saved:
            _write(home / f"{provider}-backup.json", {"provider": saved["provider"]})
    if PROVIDER not in backups:
        _write(home / f"{PROVIDER}-backup.json", {"provider": providers.get(PROVIDER)})
    rows = build_models(d)
    providers[PROVIDER] = {"name": PROVIDER, "baseUrl": cfg.base_url,
                           "api": "openai-responses", "apiKey": f"!cat {shlex.quote(str(TOKEN_FILE))}", "models": rows}
    if LEGACY_PROVIDER_ID in backups and LEGACY_PROVIDER_ID in providers:
        providers[LEGACY_PROVIDER_ID] = providers[PROVIDER].copy()  # Old Pi sessions store the provider ID.
    _write(models_path, models)
    _drop_stale_scope(home, rows)
    ui.ok(f"updated {models_path} ({len(rows)} models)")


def _read_backups(home: Path) -> dict:
    return {provider: _read(path) for provider in (PROVIDER, LEGACY_PROVIDER_ID)
            if (path := home / f"{provider}-backup.json").exists()}


def restore_pi_config(cfg: Config) -> None:
    home = agent_home()
    backups = _read_backups(home)
    if not backups:
        return
    models_path = home / "models.json"
    models = _read(models_path)
    providers = models.get("providers", {})
    _restore_legacy_scope(home, providers, backups)
    for provider, saved in backups.items():
        if saved["provider"] is None:
            providers.pop(provider, None)
        else:
            providers[provider] = saved["provider"]
    _write(models_path, models)
    for provider in backups:
        (home / f"{provider}-backup.json").unlink()
    ui.ok(f"restored Pi's previous model registry in {home}")
