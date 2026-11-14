"""Write what Codex needs: a model catalog JSON and a provider block in $CODEX_HOME/config.toml.

The Desktop app-server cannot take `--profile`, so the provider has to live in the base
config.toml. Only these keys are touched (comments and everything else are preserved):
  model_provider, model_catalog_json, [model_providers.<provider_id>]
`restore` removes them again.
"""

from __future__ import annotations

import copy
import json
import shutil
import time
from pathlib import Path
from typing import Any

import tomlkit

from . import ui
from .config import STATE_DIR, Config
from .discovery import Discovery, Route

EFFORT_TEXT = {
    "none": "No reasoning",
    "minimal": "Minimal reasoning",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balanced speed and reasoning depth",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth",
    "max": "Maximum reasoning depth",
}

# Fields that only make sense for OpenAI's backend; dropped when a GPT entry is the template.
# model_messages is kept: Codex needs it (or base_instructions) to build the system prompt.
OPENAI_ONLY = ("additional_speed_tiers", "available_access_programs",
               "multi_agent_version", "service_tiers", "tool_mode", "use_responses_lite",
               "web_search_provider", "web_search_tool_type", "comp_hash", "upgrade",
               "availability_nux", "multi_agent_reasoning_effort", "base_instructions")

FALLBACK_TEMPLATE: dict[str, Any] = {
    "shell_type": "default",
    "visibility": "list",
    "supported_in_api": True,
    "priority": 10,
    "default_verbosity": "low",
    "support_verbosity": False,
    "experimental_supported_tools": [],
    "truncation_policy": {"mode": "tokens", "limit": 10000},
}


def _custom_entry(template: dict[str, Any], r: Route, priority: int) -> dict[str, Any]:
    e = {k: v for k, v in copy.deepcopy(template).items() if k not in OPENAI_ONLY}
    e.update({
        "slug": r.slug,
        "display_name": r.display_name or r.slug,
        "description": f"{r.provider} via opencodex-desktop",
        "context_window": r.context_window,
        "max_context_window": r.context_window,
        "auto_compact_token_limit": int(r.context_window * 0.9),
        "effective_context_window_percent": 95,
        "supported_reasoning_levels": [{"effort": x, "description": EFFORT_TEXT.get(x, x)} for x in r.efforts],
        "default_reasoning_level": r.default_effort,
        "supports_reasoning_summaries": False,
        "supports_parallel_tool_calls": False,
        "support_verbosity": False,
        "supports_search_tool": False,
        "supports_reasoning_effort_updates": False,
        "input_modalities": r.input_modalities,
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
    })
    return e


def build_catalog(d: Discovery) -> dict[str, Any]:
    models = [copy.deepcopy(m) for m in d.openai_models]
    template = d.openai_models[0] if d.openai_models else FALLBACK_TEMPLATE
    base_priority = max([m.get("priority", 0) for m in models] or [0]) + 1
    models += [_custom_entry(template, r, base_priority + i) for i, r in enumerate(d.routes)]
    return {"models": models}


def write_catalog(catalog: dict[str, Any]) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "model-catalog.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog, indent=2))
    tmp.replace(path)
    return path


def _load_toml(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"config.toml.bak-opencodex-desktop-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def apply_codex_config(cfg: Config, d: Discovery, catalog_path: Path) -> None:
    path = cfg.codex_home / "config.toml"
    doc = _load_toml(path)
    before = tomlkit.dumps(doc)

    doc["model_provider"] = cfg.provider_id
    doc["model_catalog_json"] = str(catalog_path)
    providers = doc.get("model_providers")
    if providers is None:
        providers = tomlkit.table(is_super_table=True)
        doc["model_providers"] = providers
    block = tomlkit.table()
    block["name"] = "opencodex-desktop router"
    block["base_url"] = cfg.base_url
    block["wire_api"] = "responses"
    block["requires_openai_auth"] = d.openai.mode == "chatgpt"
    block["supports_websockets"] = False
    providers[cfg.provider_id] = block

    slugs = [m["slug"] for m in d.openai_models] + [r.slug for r in d.routes]
    current = doc.get("model")
    wanted = cfg.default_model
    if wanted and wanted not in slugs:
        ui.warn(f"default_model {wanted} is not available; ignoring it")
        wanted = None
    if wanted and current != wanted:
        doc["model"] = wanted
    elif current not in slugs and slugs:
        doc["model"] = slugs[0]
        ui.warn(f"current model {current!r} is not available; switched the default to {slugs[0]}")

    after = tomlkit.dumps(doc)
    if after == before:
        ui.ok(f"{path} already up to date")
        return
    bak = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after)
    ui.ok(f"updated {path}" + (f" (backup: {bak.name})" if bak else ""))


def restore_codex_config(cfg: Config, routed_slugs: set[str] | None = None) -> None:
    path = cfg.codex_home / "config.toml"
    if not path.exists():
        ui.info(f"{path} does not exist; nothing to restore")
        return
    doc = _load_toml(path)
    changed = False
    if doc.get("model_provider") == cfg.provider_id:
        del doc["model_provider"]
        changed = True
    if str(doc.get("model_catalog_json", "")).startswith(str(STATE_DIR)):
        del doc["model_catalog_json"]
        changed = True
    providers = doc.get("model_providers")
    if providers is not None and cfg.provider_id in providers:
        del providers[cfg.provider_id]
        if not providers:
            del doc["model_providers"]
        changed = True
    if doc.get("model") in (routed_slugs or set()):
        ui.info(f"model {doc['model']} only exists through the router; reset to Codex's default model")
        del doc["model"]
        changed = True
    if not changed:
        ui.ok(f"{path} has no opencodex-desktop settings")
        return
    bak = _backup(path)
    path.write_text(tomlkit.dumps(doc))
    ui.ok(f"removed opencodex-desktop settings from {path} (backup: {bak.name if bak else '-'})")
    ui.info("Codex now uses its built-in OpenAI provider; restart the Codex app to apply.")
