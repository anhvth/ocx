"""The local router Codex and Claude talk to (http://127.0.0.1:<port>).

Codex (OpenAI Responses, /v1/responses):
  a configured provider model -> that provider (sanitized); everything else -> ChatGPT's Codex
  backend (or api.openai.com for API-key logins) with Codex's own login.
Claude Code / Claude Desktop (Anthropic Messages, /v1/messages):
  claude-* -> api.anthropic.com with the client's own login (or this machine's, for Desktop);
  <provider>/<model> -> that provider's native Messages endpoint; gpt-* -> ChatGPT, translated.

Every request must carry the router token (~/.local/state/opencodex-desktop/token) as
x-opencodex-token, a Bearer token or x-api-key; Codex may instead present its own login.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import io
import json
import os
import time
import uuid
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__, ui
from .anthropic_adapter import from_messages, to_messages
from .chat_adapter import from_chat, sse_events, to_chat
from .config import ANTHROPIC_BASE, Config, router_token
from .messages_adapter import Translator, fold_system_messages, to_responses
from .discovery import _is_loopback, load_routes, provider_headers, read_openai_auth
from .transforms import (compact_v1_output, compaction_body, encode_summary, is_compaction_request,
                         response_text, sanitize_request, user_texts)

CLAUDE_MODELS = [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                 ("claude-haiku-5-5", "Claude Haiku 5.5"), ("claude-fable-5-1", "Claude Fable 5.1")]
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "accept-encoding",
       "proxy-connection", "proxy-authorization", "upgrade", "te", "trailer", "content-encoding"}


def _log(msg: str) -> None:
    if " -> " in msg and not msg.lstrip().startswith(("✓", "⚠", "✗")):
        code = msg.rsplit(" -> ", 1)[1][:3]
        msg = ("✓ " if code.startswith("2") else "✗ ") + msg
    # router.log is meant for `tail -f`, so status lines are always colored there
    print(ui.paint(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"), flush=True)


def _error(status: int, message: str, etype: str = "opencodex_desktop_error") -> web.Response:
    return web.json_response({"error": {"message": message, "type": etype}}, status=status)


def _aerror(status: int, message: str, etype: str = "api_error") -> web.Response:
    """Error in Anthropic's shape, so Claude clients show the message."""
    return web.json_response({"type": "error", "error": {"type": etype, "message": message}}, status=status)


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and hmac.compare_digest(a.encode(), b.encode())


class Router:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.routes = {r.slug: r for r in load_routes()}
        self.providers = {p.name: p for p in cfg.providers}
        self.token = router_token()
        self.direct: aiohttp.ClientSession | None = None
        self.proxied: aiohttp.ClientSession | None = None

    async def on_startup(self, _app: web.Application) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=900)
        self.direct = aiohttp.ClientSession(trust_env=False, timeout=timeout, auto_decompress=True)
        self.proxied = aiohttp.ClientSession(trust_env=True, timeout=timeout, auto_decompress=True)
        if self.cfg.providers and HEALTH_INTERVAL > 0:
            self.monitor = asyncio.create_task(self.watch_providers())

    async def on_cleanup(self, _app: web.Application) -> None:
        if getattr(self, "monitor", None):
            self.monitor.cancel()
        for s in (self.direct, self.proxied):
            if s:
                await s.close()

    async def watch_providers(self) -> None:
        """Re-discover periodically; drop unreachable models from the catalog, re-add recovered ones.

        Codex reads the catalog when an app-server/CLI session starts, so the change shows up in the
        next Codex session or Desktop relaunch; requests to a dropped model fail fast meanwhile.
        """
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                d, out = await asyncio.to_thread(_quiet_discover, self.cfg)
            except Exception as e:  # never let the monitor kill the router
                _log(f"health check failed: {e!r}")
                continue
            new = {r.slug: r for r in d.routes}
            if set(new) == set(self.routes):
                continue
            for slug in sorted(set(self.routes) - set(new)):
                _log(f"⚠ {slug} is no longer reachable; removed from the Codex model list")
            for slug in sorted(set(new) - set(self.routes)):
                _log(f"✓ {slug} is reachable again; added to the Codex model list")
            for line in out.splitlines():
                if "⚠" in line or "✗" in line:
                    _log(line.strip())
            from .codex_setup import build_catalog, write_catalog
            from .discovery import save_routes
            save_routes(d.routes)
            write_catalog(build_catalog(d))
            self.routes = new

    def session(self, url: str) -> aiohttp.ClientSession:
        assert self.direct and self.proxied
        return self.direct if _is_loopback(url) else self.proxied

    # ---------------------------------------------------------------- endpoints
    async def healthz(self, _req: web.Request) -> web.Response:
        if "cf-connecting-ip" in _req.headers:
            return web.Response(status=403)
        auth = read_openai_auth(self.cfg.codex_home)
        return web.json_response({"status": "ok", "service": "opencodex-desktop", "version": __version__,
                                  "pid": os.getpid(), "openai": auth.mode, "routes": sorted(self.routes)})

    def authorized(self, req: web.Request, claude: bool) -> bool:
        h = req.headers
        if (_same(h.get("x-opencodex-token"), self.token) or _same(h.get("x-api-key"), self.token)
                or _same(h.get("authorization"), f"Bearer {self.token}")):
            return True
        if claude:
            return False
        auth = read_openai_auth(self.cfg.codex_home)  # Codex sends its own login; only this user can read it
        cred = auth.access_token or auth.api_key
        return _same(h.get("authorization"), f"Bearer {cred}" if cred else None)

    async def handle(self, req: web.Request) -> web.StreamResponse:
        tail = req.match_info["tail"]
        raw = await req.read()
        claude = tail.startswith("v1/messages") or "anthropic-version" in req.headers
        if req.method == "HEAD":  # connectivity probes (Claude Code's HEAD /api/hello) carry no token
            return web.Response(status=200)
        if "cf-connecting-ip" in req.headers and not (claude and _same(req.headers.get("authorization"), f"Bearer {self.token}")
                                                     or claude and _same(req.headers.get("x-api-key"), self.token)
                                                     or claude and _same(req.headers.get("x-opencodex-token"), self.token)):
            # through the public tunnel (Claude app SSH sessions): only the Claude endpoints, only with the token
            _log(f"{req.method} /{tail} -> 403 (tunnel)")
            return web.Response(status=403)
        if not self.authorized(req, claude):
            msg = ("opencodex-desktop: missing or wrong router token. Claude clients: re-run "
                   "`opencodex-desktop start` (it writes the token into Claude's settings) and restart them.")
            _log(f"{req.method} /{tail} -> 401 (no router token)")
            return _aerror(401, msg, "authentication_error") if claude else _error(401, msg)
        if claude and req.method == "POST" and tail.startswith("v1/messages"):
            return await self.claude_messages(req, tail, raw)
        if claude and req.method == "GET" and tail == "v1/models":
            return web.json_response(self.claude_model_list())
        if req.method == "POST" and tail in ("v1/responses", "v1/responses/compact"):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if isinstance(body, dict) and "/" in str(body.get("model", "")) and body["model"] not in self.routes:
                alias = self.resolve_alias(body["model"])
                if alias:
                    _log(f"{body['model']} => {alias} (matched by model name)")
                    body["model"] = alias
                else:
                    return _error(404, f"opencodex-desktop: unknown or currently unreachable model {body['model']}. "
                                       f"Available: {', '.join(sorted(self.routes)) or '(none)'}. "
                                       "`opencodex-desktop status` shows what is up.", "model_not_found")
            if isinstance(body, dict) and body.get("model") in self.routes:
                return await self.routed(req, tail, body)
        return await self.forward_openai(req, tail, raw)

    def resolve_alias(self, slug: str) -> str | None:
        """`old-provider/qwen4-max-preview` -> `alibaba/qwen4-max-preview` when exactly one route serves that model
        (keeps old threads and `-m` habits working after a provider is renamed)."""
        name = slug.split("/", 1)[1]
        hits = [r.slug for r in self.routes.values() if r.upstream_model == name]
        return hits[0] if len(hits) == 1 else None

    # ---------------------------------------------------------------- Claude clients (Anthropic Messages)
    def gpt_models(self) -> list[str]:
        from .codex_setup import CATALOG
        try:
            models = json.loads(CATALOG.read_text()).get("models", [])
        except (OSError, ValueError):
            return []
        return [m["slug"] for m in models if "/" not in m.get("slug", "/")]

    def claude_model_list(self) -> dict[str, Any]:
        rows = [(m, label) for m, label in CLAUDE_MODELS]
        rows += [(s, s) for s in self.gpt_models()]
        data = [{"type": "model", "id": m, "display_name": label, "created_at": "2026-01-01T00:00:00Z"}
                for m, label in rows]
        data += [{"type": "model", "id": r.slug, "display_name": r.display_name or r.slug,
                  "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": r.context_window}
                 for r in self.routes.values()
                 if (p := self.providers.get(r.provider)) is not None and p.anthropic_url]
        return {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None}

    async def claude_messages(self, req: web.Request, tail: str, raw: bytes) -> web.StreamResponse:
        try:
            body = json.loads(raw)
        except ValueError:
            return _aerror(400, "invalid JSON body", "invalid_request_error")
        model = str(body.get("model", "")).removesuffix("[1m]")
        if model.startswith("anthropic/"):
            model = model.split("/", 1)[1]
        count = tail.endswith("/count_tokens")
        if "/" in model:
            if model not in self.routes:
                alias = self.resolve_alias(model)
                if not alias:
                    return _aerror(404, f"opencodex-desktop: unknown or currently unreachable model {model}. "
                                        f"Available: {', '.join(sorted(self.routes)) or '(none)'}", "not_found_error")
                model = alias
            route = self.routes[model]
            if count:
                return web.json_response({"input_tokens": len(json.dumps(body)) // 4})
            return await self.claude_routed(req, body, route)
        if model.startswith("claude"):
            body["model"] = model
            return await self.claude_native(req, tail, body)
        if count:
            return web.json_response({"input_tokens": len(json.dumps(body)) // 4})
        return await self.claude_gpt(req, body, model)

    async def claude_native(self, req: web.Request, tail: str, body: dict[str, Any]) -> web.StreamResponse:
        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP and k.lower() != "x-opencodex-token"}
        lower = {k.lower(): k for k in headers}
        auth_hdr = headers.get(lower.get("authorization", ""), "")
        api_hdr = headers.get(lower.get("x-api-key", ""), "")
        base = ANTHROPIC_BASE  # a client's own login only ever goes to api.anthropic.com
        if _same(auth_hdr, f"Bearer {self.token}") or _same(api_hdr, self.token) or not (auth_hdr or api_hdr):
            # Claude desktop app (gateway mode) authenticates with the router token. A Claude
            # subscription login is only for Claude Code itself, so this needs an Anthropic API key.
            anth = self.providers.get("anthropic")
            if not (anth and anth.api_key):
                return _aerror(401, "opencodex-desktop: claude-* through the gateway needs an Anthropic API key "
                                    "(set ANTHROPIC_API_KEY or `anthropic: {api_key_file: ...}`, then start again)",
                               "authentication_error")
            for k in ("authorization", "x-api-key"):
                headers.pop(lower.get(k, k), None)
            headers["x-api-key"] = anth.api_key
            if "anthropic-version" not in lower:
                headers["anthropic-version"] = "2023-06-01"
            base = anth.base_url
        url = f"{base.rsplit('/v1', 1)[0]}/{tail}" + (f"?{req.query_string}" if req.query_string else "")
        return await self.stream(req, "POST", url, headers, json.dumps(body).encode(), label=f"claude {body['model']}")

    async def claude_routed(self, req: web.Request, body: dict[str, Any], route: Any) -> web.StreamResponse:
        provider = self.providers.get(route.provider)
        if provider is None or not provider.anthropic_url:
            return _aerror(400, f"opencodex-desktop: provider {route.provider} has no Anthropic Messages endpoint; "
                                "set `anthropic_url:` for it in the config", "invalid_request_error")
        headers = {"content-type": "application/json",
                   "anthropic-version": req.headers.get("anthropic-version", "2023-06-01")}
        if provider.api_key:
            headers["x-api-key"] = provider.api_key
            headers["authorization"] = f"Bearer {provider.api_key}"
        headers.update(provider.headers)
        body = {**body, "model": route.upstream_model, "messages": fold_system_messages(body.get("messages") or [])}
        for k in ("context_management", "container", "mcp_servers"):
            body.pop(k, None)
        if os.environ.get("OPENCODEX_DESKTOP_DUMP_DIR"):  # debugging only: bodies contain prompts
            with open(os.path.join(os.environ["OPENCODEX_DESKTOP_DUMP_DIR"], f"{time.time():.3f}.json"), "w") as f:
                json.dump(body, f)
        return await self.stream(req, "POST", f"{provider.anthropic_url}/messages", headers,
                                 json.dumps(body).encode(), label=f"{route.slug} (claude)")

    async def claude_gpt(self, req: web.Request, body: dict[str, Any], model: str) -> web.StreamResponse:
        auth = read_openai_auth(self.cfg.codex_home)
        if auth.mode == "none" or not auth.upstream:
            return _aerror(401, "opencodex-desktop: not logged in to Codex; run `codex login`", "authentication_error")
        headers = {"content-type": "application/json", "accept": "text/event-stream",
                   "Authorization": f"Bearer {auth.access_token or auth.api_key}"}
        if auth.mode == "chatgpt" and auth.account_id:
            headers["ChatGPT-Account-ID"] = auth.account_id
        payload = to_responses(body, model)
        url = f"{auth.upstream}/responses"
        start = time.monotonic()
        tr = Translator(model)
        streaming = bool(body.get("stream"))
        resp: web.StreamResponse | None = None
        try:
            async with self.session(url).post(url, headers=headers, json=payload) as up:
                if up.status >= 400:
                    text = (await up.text())[:1000]
                    _log(f"{model} (claude) -> {up.status} {text[:300]}")
                    return _aerror(up.status, f"ChatGPT: {text}")
                if streaming:
                    resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream",
                                                                   "cache-control": "no-cache"})
                    await resp.prepare(req)
                    await resp.write(tr.start())
                buf = b""
                async for chunk in up.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        frame, buf = buf.split(b"\n\n", 1)
                        data = b"".join(line[5:].strip() for line in frame.split(b"\n") if line.startswith(b"data:"))
                        if not data or data == b"[DONE]":
                            continue
                        try:
                            out = tr.feed(json.loads(data))
                        except ValueError:
                            continue
                        if out and resp is not None:
                            await resp.write(out)
        except (aiohttp.ClientError, TimeoutError, ConnectionResetError) as e:
            if resp is not None and resp.prepared:
                _log(f"{model} (claude) -> 200 ({time.monotonic() - start:.1f}s, client closed early)")
                return resp
            _log(f"{model} (claude) -> unreachable: {e!r}")
            return _aerror(502, f"opencodex-desktop: cannot reach ChatGPT: {e}")
        _log(f"{model} (claude) -> {'200' if not tr.error else '502 ' + tr.error} ({time.monotonic() - start:.1f}s)")
        if resp is not None:
            await resp.write(tr.finish())
            await resp.write_eof()
            return resp
        if tr.error:
            return _aerror(502, f"ChatGPT: {tr.error}")
        return web.json_response(tr.message())

    # ---------------------------------------------------------------- ChatGPT / OpenAI
    async def forward_openai(self, req: web.Request, tail: str, raw: bytes) -> web.StreamResponse:
        auth = read_openai_auth(self.cfg.codex_home)
        if auth.mode == "none" or not auth.upstream:
            return _error(401, "opencodex-desktop: not logged in to Codex; run `codex login` (or sign in in the Codex app)")
        sub = tail[3:] if tail.startswith("v1/") else tail
        url = f"{auth.upstream}/{sub}" + (f"?{req.query_string}" if req.query_string else "")
        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
        lower = {k.lower() for k in headers}
        if "authorization" not in lower:
            headers["Authorization"] = f"Bearer {auth.access_token or auth.api_key}"
        if auth.mode == "chatgpt" and "chatgpt-account-id" not in lower and auth.account_id:
            headers["ChatGPT-Account-ID"] = auth.account_id
        label = f"openai {sub}"
        if sub == "responses" and raw:
            try:
                b = json.loads(raw)
                label += f" {b.get('model')}" + (f" tier={b['service_tier']}" if b.get("service_tier") else "")
            except ValueError:
                pass
        return await self.stream(req, req.method, url, headers, raw, label=label)

    async def stream(self, req: web.Request, method: str, url: str, headers: dict[str, str],
                     data: bytes, label: str) -> web.StreamResponse:
        start = time.monotonic()
        resp: web.StreamResponse | None = None
        try:
            async with self.session(url).request(method, url, headers=headers, data=data) as up:
                resp = web.StreamResponse(status=up.status, headers={
                    k: v for k, v in up.headers.items() if k.lower() not in HOP})
                await resp.prepare(req)
                head = b""
                async for chunk in up.content.iter_any():
                    if up.status >= 400 and len(head) < 2000:
                        head += chunk
                    await resp.write(chunk)
                await resp.write_eof()
                _log(f"{label} -> {up.status} ({time.monotonic() - start:.1f}s)"
                     + (f" {head[:500].decode(errors='replace')}" if up.status >= 400 else ""))
                return resp
        except (aiohttp.ClientError, TimeoutError, ConnectionResetError) as e:
            if resp is not None and resp.prepared:
                # Codex hangs up as soon as it has response.completed; not an upstream problem.
                _log(f"{label} -> {resp.status} ({time.monotonic() - start:.1f}s, client closed early)")
                return resp
            _log(f"{label} -> unreachable: {e!r}")
            return _error(502, f"opencodex-desktop: cannot reach {url.split('?')[0]}: {e}", "upstream_unreachable")

    # ---------------------------------------------------------------- routed providers
    async def routed(self, req: web.Request, tail: str, body: dict[str, Any]) -> web.StreamResponse:
        route = self.routes[body["model"]]
        provider = self.providers.get(route.provider)
        if provider is None:
            return _error(500, f"opencodex-desktop: provider {route.provider} missing from config; re-run start")
        headers = {**provider_headers(provider), "content-type": "application/json"}

        if tail.endswith("/compact") or is_compaction_request(body):
            try:
                summary = await self.summarize(body, route, headers)
            except UpstreamError as e:
                return _error(e.status, f"{route.slug} compaction failed: {e.message}")
            _log(f"{route.slug} compaction -> {len(summary)} chars")
            if tail.endswith("/compact"):
                kept = user_texts(sanitize_request(body, route).get("input") or [])
                return web.json_response({"output": compact_v1_output(kept, summary)})
            item = {"type": "compaction", "id": f"cmp_{uuid.uuid4().hex[:24]}", "encrypted_content": encode_summary(summary)}
            return await self.replay(req, body, route, [item], None)

        b = sanitize_request(body, route)
        if route.wire in ("chat", "anthropic"):
            try:
                items, usage = await (self.anthropic_call(b, route, headers) if route.wire == "anthropic"
                                      else self.chat_call(b, route, headers))
            except UpstreamError as e:
                return _error(e.status, e.message)
            return await self.replay(req, body, route, items, usage)
        data = json.dumps(b).encode()
        return await self.stream(req, "POST", f"{route.base_url}/responses", headers, data, label=route.slug)

    async def replay(self, req: web.Request, body: dict[str, Any], route: Any,
                     items: list[dict[str, Any]], usage: dict[str, Any] | None) -> web.StreamResponse:
        events = sse_events(route.slug, items, usage)
        if not body.get("stream"):
            final = json.loads(events[-1].decode().split("data: ", 1)[1])
            return web.json_response(final["response"])
        resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream", "cache-control": "no-cache"})
        await resp.prepare(req)
        for e in events:
            await resp.write(e)
        await resp.write_eof()
        _log(f"{route.slug} -> 200 (replayed {len(items)} items)")
        return resp

    async def post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self.session(url).post(url, headers=headers, json=payload) as up:
                text = await up.text()
                if up.status >= 400:
                    raise UpstreamError(up.status, text[:1000])
                return json.loads(text)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise UpstreamError(502, f"cannot reach {url}: {e}") from e

    async def chat_call(self, b: dict[str, Any], route: Any, headers: dict[str, str]):
        chat, custom = to_chat(b)
        resp = await self.post_json(f"{route.base_url}/chat/completions", headers, chat)
        return from_chat(resp, custom)

    async def anthropic_call(self, b: dict[str, Any], route: Any, headers: dict[str, str]):
        payload, custom = to_messages(b, route)
        resp = await self.post_json(f"{route.base_url}/messages", headers, payload)
        return from_messages(resp, custom)

    async def summarize(self, body: dict[str, Any], route: Any, headers: dict[str, str]) -> str:
        cb = compaction_body(body, route)
        if route.wire in ("chat", "anthropic"):
            items, _ = await (self.anthropic_call(cb, route, headers) if route.wire == "anthropic"
                              else self.chat_call(cb, route, headers))
            return "\n".join(c.get("text", "") for i in items if i["type"] == "message" for c in i["content"])
        return response_text(await self.post_json(f"{route.base_url}/responses", headers, cb))


HEALTH_INTERVAL = int(os.environ.get("OPENCODEX_DESKTOP_HEALTH_INTERVAL", "60"))


def _quiet_discover(cfg: Config):
    from .discovery import discover
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d = discover(cfg)
    return d, buf.getvalue()


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def build_app(cfg: Config) -> web.Application:
    router = Router(cfg)
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.on_startup.append(router.on_startup)
    app.on_cleanup.append(router.on_cleanup)
    app.router.add_get("/healthz", router.healthz)
    app.router.add_route("*", "/{tail:.*}", router.handle)
    return app


def serve(cfg: Config) -> None:
    app = build_app(cfg)
    _log(f"opencodex-desktop {__version__} listening on {cfg.listen_host}:{cfg.listen_port}; "
         f"{len(load_routes())} routed models; codex home {cfg.codex_home}")
    web.run_app(app, host=cfg.listen_host, port=cfg.listen_port, print=None, access_log=None)
