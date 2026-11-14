"""The local router Codex talks to (config.toml base_url = http://127.0.0.1:<port>/v1).

POST /v1/responses[/compact] for a configured provider model -> that provider (sanitized).
Everything else -> ChatGPT's Codex backend (or api.openai.com for API-key logins), forwarding
the Authorization header Codex sends, so GPT models keep using your normal Codex login.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .chat_adapter import from_chat, sse_events, to_chat
from .config import Config
from .discovery import _is_loopback, load_routes, provider_headers, read_openai_auth
from .transforms import (compact_v1_output, compaction_body, encode_summary, is_compaction_request,
                         response_text, sanitize_request, user_texts)

HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "accept-encoding",
       "proxy-connection", "proxy-authorization", "upgrade", "te", "trailer", "content-encoding"}


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _error(status: int, message: str, etype: str = "opencodex_desktop_error") -> web.Response:
    return web.json_response({"error": {"message": message, "type": etype}}, status=status)


class Router:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.routes = {r.slug: r for r in load_routes()}
        self.providers = {p.name: p for p in cfg.providers}
        self.direct: aiohttp.ClientSession | None = None
        self.proxied: aiohttp.ClientSession | None = None

    async def on_startup(self, _app: web.Application) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=900)
        self.direct = aiohttp.ClientSession(trust_env=False, timeout=timeout, auto_decompress=True)
        self.proxied = aiohttp.ClientSession(trust_env=True, timeout=timeout, auto_decompress=True)

    async def on_cleanup(self, _app: web.Application) -> None:
        for s in (self.direct, self.proxied):
            if s:
                await s.close()

    def session(self, url: str) -> aiohttp.ClientSession:
        assert self.direct and self.proxied
        return self.direct if _is_loopback(url) else self.proxied

    # ---------------------------------------------------------------- endpoints
    async def healthz(self, _req: web.Request) -> web.Response:
        auth = read_openai_auth(self.cfg.codex_home)
        return web.json_response({"status": "ok", "service": "opencodex-desktop", "version": __version__,
                                  "pid": os.getpid(), "openai": auth.mode, "routes": sorted(self.routes)})

    async def handle(self, req: web.Request) -> web.StreamResponse:
        tail = req.match_info["tail"]
        raw = await req.read()
        if req.method == "POST" and tail in ("v1/responses", "v1/responses/compact"):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("model") in self.routes:
                return await self.routed(req, tail, body)
        return await self.forward_openai(req, tail, raw)

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
        if route.wire == "chat":
            try:
                items, usage = await self.chat_call(b, route, headers)
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

    async def summarize(self, body: dict[str, Any], route: Any, headers: dict[str, str]) -> str:
        cb = compaction_body(body, route)
        if route.wire == "chat":
            items, _ = await self.chat_call(cb, route, headers)
            return "\n".join(c.get("text", "") for i in items if i["type"] == "message" for c in i["content"])
        return response_text(await self.post_json(f"{route.base_url}/responses", headers, cb))


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
