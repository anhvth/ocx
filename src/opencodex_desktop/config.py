"""Load the single YAML config and resolve paths, keys and defaults.

Everything is optional: with no config file the tool serves only your ChatGPT models
(read from the Codex login in $CODEX_HOME). See examples/config.yaml for every field.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "opencodex-desktop" / "config.yaml"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "opencodex-desktop"
DEFAULT_LISTEN = "127.0.0.1:11111"
DEFAULT_PROVIDER_ID = "opencodex-desktop"
DEFAULT_CONTEXT = 128_000
DEFAULT_EFFORTS = ["low", "medium", "high"]
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_CONTEXT = 200_000


class ConfigError(Exception):
    pass


@dataclass
class ModelCfg:
    id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning_efforts: list[str] | None = None
    default_effort: str | None = None
    input_modalities: list[str] | None = None


@dataclass
class ProviderCfg:
    name: str
    base_url: str
    api_key: str | None = None
    api_key_source: str = "none"
    wire_api: str | None = None  # "responses" | "chat" | None (auto-detect)
    models_auto: bool = False
    include: list[str] = field(default_factory=list)
    models: list[ModelCfg] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Config:
    path: Path | None
    codex_home: Path
    listen_host: str
    listen_port: int
    provider_id: str
    openai_enabled: bool
    openai_include: list[str]
    default_model: str | None
    providers: list[ProviderCfg]

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}/v1"


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def resolve_codex_home(value: Any) -> Path:
    """$CODEX_HOME wins; else the first existing candidate from the config; else ~/.codex."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return _expand(env)
    candidates = value if isinstance(value, list) else ([value] if value else [])
    for c in candidates:
        p = _expand(str(c))
        if (p / "config.toml").exists() or (p / "auth.json").exists():
            return p
    return Path("~/.codex").expanduser()


def _resolve_key(name: str, raw: dict[str, Any]) -> tuple[str | None, str]:
    if raw.get("api_key"):
        return str(raw["api_key"]).strip(), "inline api_key"
    if raw.get("api_key_env"):
        var = str(raw["api_key_env"])
        val = os.environ.get(var)
        if not val:
            raise ConfigError(f"provider '{name}': environment variable {var} is not set")
        return val.strip(), f"${var}"
    if raw.get("api_key_file"):
        p = _expand(str(raw["api_key_file"]))
        if not p.exists():
            raise ConfigError(f"provider '{name}': api_key_file {p} does not exist")
        return p.read_text().strip(), str(p)
    return None, "none"


def _model(entry: Any) -> ModelCfg:
    if isinstance(entry, str):
        return ModelCfg(id=entry)
    if isinstance(entry, dict) and len(entry) == 1 and isinstance(next(iter(entry.values())), (dict, type(None))):
        mid, opts = next(iter(entry.items()))
        opts = opts or {}
    elif isinstance(entry, dict) and "id" in entry:
        mid, opts = entry["id"], entry
    else:
        raise ConfigError(f"cannot parse model entry: {entry!r}")
    return ModelCfg(
        id=str(mid),
        display_name=opts.get("display_name"),
        context_window=opts.get("context_window"),
        max_output_tokens=opts.get("max_output_tokens"),
        reasoning_efforts=opts.get("reasoning_efforts"),
        default_effort=opts.get("default_effort"),
        input_modalities=opts.get("input_modalities"),
    )


def _provider(name: str, pr: dict[str, Any]) -> ProviderCfg:
    if "/" in name:
        raise ConfigError(f"provider name '{name}' must not contain '/'")
    if not pr.get("base_url"):
        raise ConfigError(f"provider '{name}': base_url is required")
    key, source = _resolve_key(name, pr)
    models_raw = pr.get("models", "auto")
    auto = models_raw in ("auto", None)
    wire = pr.get("wire_api")
    if wire is None and "anthropic.com" in str(pr["base_url"]):
        wire = "anthropic"
    if wire not in (None, "responses", "chat", "anthropic"):
        raise ConfigError(f"provider '{name}': wire_api must be responses, chat or anthropic")
    return ProviderCfg(
        name=name,
        base_url=str(pr["base_url"]).rstrip("/"),
        api_key=key,
        api_key_source=source,
        wire_api=wire,
        models_auto=auto,
        include=list(pr.get("include") or []),
        models=[] if auto else [_model(m) for m in models_raw],
        headers={str(k): str(v) for k, v in (pr.get("headers") or {}).items()},
        enabled=pr.get("enabled", True) is not False,
    )


def _anthropic_provider(raw: dict[str, Any], taken: set[str]) -> ProviderCfg | None:
    """Built-in claude-* support: an `anthropic:` block, or just $ANTHROPIC_API_KEY."""
    block = raw.get("anthropic")
    if "anthropic" in taken or (block is None and "ANTHROPIC_API_KEY" not in os.environ):
        return None
    block = dict(block or {})
    if block.get("enabled", True) is False:
        return None
    block.setdefault("base_url", ANTHROPIC_BASE)
    block.setdefault("wire_api", "anthropic")
    if not any(k in block for k in ("api_key", "api_key_env", "api_key_file")):
        block["api_key_env"] = "ANTHROPIC_API_KEY"
    return _provider("anthropic", block)


def load(path: str | os.PathLike[str] | None = None) -> Config:
    explicit = path is not None or "OPENCODEX_DESKTOP_CONFIG" in os.environ
    p = _expand(str(path or os.environ.get("OPENCODEX_DESKTOP_CONFIG") or DEFAULT_CONFIG))
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
    elif explicit:
        raise ConfigError(f"config file {p} does not exist")

    listen = str(raw.get("listen", DEFAULT_LISTEN))
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError(f"listen must look like 127.0.0.1:11111, got {listen!r}")

    openai = raw.get("openai") or {}
    providers: list[ProviderCfg] = []
    for name, pr in (raw.get("providers") or {}).items():
        providers.append(_provider(name, pr or {}))
    anth = _anthropic_provider(raw, {p.name for p in providers})
    if anth is not None:
        providers.append(anth)

    return Config(
        path=p if p.exists() else None,
        codex_home=resolve_codex_home(raw.get("codex_home")),
        listen_host=host,
        listen_port=int(port),
        provider_id=str(raw.get("provider_id", DEFAULT_PROVIDER_ID)),
        openai_enabled=openai.get("enabled", True) is not False,
        openai_include=list(openai.get("include") or []),
        default_model=raw.get("default_model"),
        providers=providers,
    )
