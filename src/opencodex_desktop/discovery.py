"""Find out which models are actually usable right now.

- ChatGPT models: from the Codex login in $CODEX_HOME (auth.json) and the model list Codex
  caches there (models_cache.json). Not logged in -> no GPT models.
- Providers: GET {base_url}/models. Unreachable provider or model not served -> dropped.
  Context window: config value, else whatever /models reports (vLLM max_model_len, ...),
  else a conservative default with a warning.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import ui
from .config import DEFAULT_CONTEXT, DEFAULT_EFFORTS, STATE_DIR, Config, ProviderCfg

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
OPENAI_API_BASE = "https://api.openai.com/v1"
CONTEXT_KEYS = ("context_window", "max_model_len", "context_length", "max_context_length",
                "max_input_tokens", "n_ctx", "n_ctx_train")


@dataclass
class OpenAIAuth:
    mode: str  # "chatgpt" | "apikey" | "none"
    upstream: str | None = None
    account_id: str | None = None
    access_token: str | None = None
    api_key: str | None = None
    email: str | None = None
    expires_at: float | None = None


@dataclass
class Route:
    slug: str
    provider: str
    upstream_model: str
    base_url: str
    wire: str
    context_window: int
    context_source: str
    max_output_tokens: int | None = None
    efforts: list[str] = field(default_factory=lambda: list(DEFAULT_EFFORTS))
    default_effort: str = "medium"
    display_name: str | None = None
    input_modalities: list[str] = field(default_factory=lambda: ["text"])


@dataclass
class Discovery:
    openai: OpenAIAuth
    openai_models: list[dict[str, Any]]
    routes: list[Route]
    errors: int = 0


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("localhost", "::1") or host.startswith("127.")


def http_json(method: str, url: str, headers: dict[str, str] | None = None,
              body: Any = None, timeout: float = 10) -> tuple[int, Any]:
    """Tiny JSON HTTP client. Loopback URLs never go through HTTP(S)_PROXY."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "content-type": "application/json", "accept": "application/json", **(headers or {})})
    handlers = [urllib.request.ProxyHandler({})] if _is_loopback(url) else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode(errors="replace")[:500]


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def read_openai_auth(codex_home: Path) -> OpenAIAuth:
    path = codex_home / "auth.json"
    try:
        auth = json.loads(path.read_text())
    except (OSError, ValueError):
        return OpenAIAuth(mode="none")
    tokens = auth.get("tokens") or {}
    if tokens.get("access_token"):
        claims = _jwt_claims(tokens["access_token"])
        id_claims = _jwt_claims(tokens.get("id_token") or "")
        acct = tokens.get("account_id") or (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
        return OpenAIAuth(mode="chatgpt", upstream=CHATGPT_BASE, account_id=acct,
                          access_token=tokens["access_token"], email=id_claims.get("email"),
                          expires_at=claims.get("exp"))
    if auth.get("OPENAI_API_KEY"):
        return OpenAIAuth(mode="apikey", upstream=OPENAI_API_BASE, api_key=auth["OPENAI_API_KEY"])
    return OpenAIAuth(mode="none")


def _openai_models(cfg: Config, auth: OpenAIAuth) -> list[dict[str, Any]]:
    cache = cfg.codex_home / "models_cache.json"
    models: list[dict[str, Any]] = []
    try:
        models = json.loads(cache.read_text()).get("models") or []
    except (OSError, ValueError):
        pass
    if not models and auth.mode == "chatgpt":
        status, data = http_json("GET", f"{CHATGPT_BASE}/models?client_version=0.169.0", headers={
            "authorization": f"Bearer {auth.access_token}", "chatgpt-account-id": auth.account_id or ""})
        if status == 200 and isinstance(data, dict):
            models = data.get("models") or []
    # Real OpenAI slugs never contain "/"; such entries were cached from another gateway's catalog.
    models = [m for m in models if m.get("visibility", "list") == "list" and m.get("slug") and "/" not in m["slug"]]
    if cfg.openai_include:
        models = [m for m in models if m["slug"] in cfg.openai_include]
    return models


def _context_from(entry: dict[str, Any]) -> int | None:
    for src in (entry, entry.get("meta") or {}, entry.get("metadata") or {}):
        for k in CONTEXT_KEYS:
            v = src.get(k)
            if isinstance(v, int) and v > 0:
                return v
    return None


def _wire_cache() -> dict[str, str]:
    try:
        return json.loads((STATE_DIR / "wire-cache.json").read_text())
    except (OSError, ValueError):
        return {}


def _detect_wire(p: ProviderCfg, model: str, headers: dict[str, str]) -> str:
    if p.wire_api:
        return p.wire_api
    cache = _wire_cache()
    if p.base_url in cache:
        return cache[p.base_url]
    status, _ = http_json("POST", f"{p.base_url}/responses", headers=headers, timeout=60, body={
        "model": model, "input": "Reply with: ok", "max_output_tokens": 16, "stream": False})
    wire = "chat" if status in (404, 405, 501) else "responses"
    cache[p.base_url] = wire
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "wire-cache.json").write_text(json.dumps(cache, indent=2))
    return wire


def provider_headers(p: ProviderCfg) -> dict[str, str]:
    h = dict(p.headers)
    if p.api_key:
        h["authorization"] = f"Bearer {p.api_key}"
    return h


def _discover_provider(p: ProviderCfg) -> tuple[list[Route], bool]:
    headers = provider_headers(p)
    ui.head(f"{p.name}  ({p.base_url}, key: {p.api_key_source})")
    try:
        status, data = http_json("GET", f"{p.base_url}/models", headers=headers, timeout=10)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        ui.err(f"unreachable: {getattr(e, 'reason', e)} -> all {p.name} models dropped")
        return [], False
    served: dict[str, dict[str, Any]] | None = None
    if status in (401, 403):
        ui.err(f"authentication failed (HTTP {status}); check the api key ({p.api_key_source}) -> dropped")
        return [], False
    if status == 200 and isinstance(data, dict) and isinstance(data.get("data"), list):
        served = {str(m.get("id")): m for m in data["data"] if m.get("id")}
    elif status == 404:
        ui.warn("provider has no /models endpoint; trusting the configured model list")
    else:
        ui.err(f"/models returned HTTP {status}: {str(data)[:200]} -> dropped")
        return [], False

    if p.models_auto:
        if served is None:
            ui.err("models: auto needs a /models endpoint; list the models explicitly")
            return [], False
        from .config import ModelCfg
        wanted = [ModelCfg(id=mid) for mid in served if not p.include or any(s in mid for s in p.include)]
    else:
        wanted = p.models

    routes: list[Route] = []
    for m in wanted:
        if served is not None and m.id not in served:
            ui.warn(f"{m.id}: not served by the provider -> dropped")
            continue
        listed = _context_from(served.get(m.id, {})) if served else None
        if m.context_window:
            ctx, src = int(m.context_window), "config"
            if listed and listed < ctx:
                ui.warn(f"{m.id}: config context {ui.fmt_tokens(ctx)} > provider-reported {ui.fmt_tokens(listed)}; using {ui.fmt_tokens(listed)}")
                ctx, src = listed, "provider"
        elif listed:
            ctx, src = listed, "provider /models"
        else:
            ctx, src = DEFAULT_CONTEXT, "default"
            ui.warn(f"{m.id}: context window unknown; using {ui.fmt_tokens(ctx)} (set context_window in the config)")
        try:
            wire = _detect_wire(p, m.id, headers)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            ui.err(f"{m.id}: probe failed ({e}) -> dropped")
            continue
        efforts = m.reasoning_efforts or list(DEFAULT_EFFORTS)
        default = m.default_effort or ("medium" if "medium" in efforts else efforts[-1])
        routes.append(Route(
            slug=f"{p.name}/{m.id}", provider=p.name, upstream_model=m.id, base_url=p.base_url,
            wire=wire, context_window=ctx, context_source=src, max_output_tokens=m.max_output_tokens,
            efforts=efforts, default_effort=default, display_name=m.display_name,
            input_modalities=m.input_modalities or ["text"],
        ))
        if ctx < 32_000:
            ui.warn(f"{m.id}: context {ui.fmt_tokens(ctx)} is smaller than Codex's own prompt+tools (~20k); it will not work as an agent")
        ui.ok(f"{p.name}/{m.id}  ctx {ui.fmt_tokens(ctx)} ({src})  wire {wire}")
    return routes, True


def discover(cfg: Config) -> Discovery:
    errors = 0
    ui.head(f"Codex home: {cfg.codex_home}")
    auth = read_openai_auth(cfg.codex_home)
    openai_models: list[dict[str, Any]] = []
    if not cfg.openai_enabled:
        ui.info("ChatGPT models disabled in config")
    elif auth.mode == "none":
        ui.warn("not logged in to Codex (no auth.json): GPT models skipped. Run `codex login` or sign in in the Codex app.")
    else:
        openai_models = _openai_models(cfg, auth)
        who = auth.email or auth.account_id or "API key"
        if auth.mode == "chatgpt" and auth.expires_at and auth.expires_at < time.time():
            ui.info("access token expired; Codex refreshes it on the next request")
        if openai_models:
            ui.ok(f"ChatGPT ({auth.mode}, {who}): {len(openai_models)} models: " + ", ".join(m["slug"] for m in openai_models))
        else:
            ui.warn("logged in, but no model list cached yet; open the Codex app or run `codex` once, then re-run start")

    routes: list[Route] = []
    for p in cfg.providers:
        if not p.enabled:
            continue
        r, ok = _discover_provider(p)
        routes += r
        errors += 0 if ok else 1
    return Discovery(openai=auth, openai_models=openai_models, routes=routes, errors=errors)


def save_routes(routes: list[Route]) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "routes.json"
    path.write_text(json.dumps([asdict(r) for r in routes], indent=2))
    return path


def load_routes() -> list[Route]:
    try:
        return [Route(**r) for r in json.loads((STATE_DIR / "routes.json").read_text())]
    except (OSError, ValueError, TypeError):
        return []
