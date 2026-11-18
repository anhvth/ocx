import asyncio
import json
import urllib.error
from types import SimpleNamespace

import aiohttp
from aiohttp import web

from opencodex_desktop import codex_setup, router, usage
from opencodex_desktop.discovery import Route
from opencodex_desktop.landing import USAGE_URL, render_landing


def test_render_landing_escapes_model_slugs():
    page = render_landing("1.2.3", "http://127.0.0.1:11111/v1", [{
        "provider": "example",
        "models": [{"slug": "example/model", "context": 300_000, "images": True, "reasoning": True},
                   {"slug": "model<unsafe>", "context": 1_000_000, "images": False, "reasoning": False}],
    }])
    assert "example/model" in page
    assert "model&lt;unsafe&gt;" in page
    assert "model<unsafe>" not in page
    assert "300K" in page and "1M" in page


def test_landing_is_public_while_api_stays_protected(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"models": []}')
    monkeypatch.setattr(codex_setup, "CATALOG", catalog)
    monkeypatch.setattr(router, "router_token", lambda: "test-only-router-token")
    routed = Route(slug="vendor/routed-model", provider="vendor", upstream_model="routed-model",
                   base_url="http://upstream/v1", wire="responses", context_window=300_000,
                   context_source="test", input_modalities=["text", "image"])
    monkeypatch.setattr(router, "load_routes", lambda: [routed])
    cfg = SimpleNamespace(codex_home=tmp_path, providers=[], openai_enabled=False, listen_host="127.0.0.1", listen_port=19999,
                          base_url="http://127.0.0.1:19999/v1")

    async def run():
        app = router.build_app(cfg)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/") as response:
                    body = await response.text()
                    assert response.status == 200
                    assert response.content_type == "text/html"
                    assert "vendor/routed-model" in body
                    assert "test-only-router-token" not in body
                async with session.post(f"http://127.0.0.1:{port}/v1/responses", json={"model": "gpt-test"}) as response:
                    assert response.status == 401
        finally:
            await runner.cleanup()

    asyncio.run(run())


_USAGE_BODY = {"plan_type": "pro", "email": "secret@example.com", "user_id": "u-1",
               "rate_limit": {"primary_window": {"used_percent": 86, "limit_window_seconds": 604800,
                                                  "reset_at": 1794973752}, "secondary_window": None},
               "credits": {"has_credits": True, "unlimited": False, "balance": "55120.76"},
               "rate_limit_reset_credits": {"available_count": 2}}


class _FakeResponse:
    def __init__(self, body): self._body = json.dumps(body).encode()
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_fetch_usage_sanitizes_and_formats(monkeypatch):
    monkeypatch.setattr(usage, "_CACHE", {"at": 0.0, "value": None})
    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(_USAGE_BODY))
    auth = SimpleNamespace(mode="chatgpt", upstream="https://x/backend-api/codex",
                           access_token="t", api_key=None, account_id="a")
    out = usage.fetch_codex_usage(auth)
    assert out["plan"] == "pro" and out["windows"][0]["left"] == 14
    assert out["credits"] == "55,121 credits" and out["resets"] == 2
    assert "secret@example.com" not in json.dumps(out) and "email" not in out


def test_fetch_usage_unreachable_returns_none(monkeypatch):
    monkeypatch.setattr(usage, "_CACHE", {"at": 0.0, "value": None})
    def boom(req, timeout): raise urllib.error.URLError("down")
    monkeypatch.setattr(usage.urllib.request, "urlopen", boom)
    auth = SimpleNamespace(mode="chatgpt", upstream="https://x", access_token="t", api_key=None, account_id=None)
    assert usage.fetch_codex_usage(auth) is None


def test_render_usage_panel_and_escape():
    u = {"plan": "pro", "windows": [{"label": "Weekly limit", "left": 14, "reset_at": 1794973752, "seconds": 604800}],
         "credits": "55,121 credits", "resets": 2}
    page = render_landing("1.0", "http://x/v1", [], u)
    assert "Weekly limit" in page and "14% left" in page and "55,121 credits" in page
    assert "2 remaining" in page and "Your Pro plan" in page
    assert USAGE_URL in page and 'target="_blank"' in page and 'rel="noopener noreferrer"' in page
    assert "Reset / manage usage" in page
    hostile = render_landing("1.0", "http://x/v1", [], {**u, "plan": "pro<script>"}).lower()
    assert "&lt;script&gt;" in hostile and "pro<script>" not in hostile
    none_page = render_landing("1.0", "http://x/v1", [], None)
    assert "Codex usage unavailable" in none_page and USAGE_URL in none_page


def test_landing_shows_usage_without_leaking_token(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"; catalog.write_text('{"models": []}')
    monkeypatch.setattr(codex_setup, "CATALOG", catalog)
    monkeypatch.setattr(router, "router_token", lambda: "test-only-router-token")
    monkeypatch.setattr(router, "load_routes", lambda: [])
    monkeypatch.setattr(router, "fetch_codex_usage", lambda auth: {
        "plan": "pro", "windows": [{"label": "Weekly limit", "left": 14, "reset_at": 1794973752, "seconds": 604800}],
        "credits": "55,121 credits", "resets": 2})
    cfg = SimpleNamespace(codex_home=tmp_path, providers=[], openai_enabled=False, listen_host="127.0.0.1", listen_port=19999,
                          base_url="http://127.0.0.1:19999/v1")

    async def run():
        app = router.build_app(cfg)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/") as r:
                    body = await r.text()
                    assert r.status == 200 and "Weekly limit" in body and "14% left" in body
                    assert "test-only-router-token" not in body
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_overview_api_is_public_json_without_secrets(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"; catalog.write_text('{"models": []}')
    monkeypatch.setattr(codex_setup, "CATALOG", catalog)
    monkeypatch.setattr(router, "router_token", lambda: "test-only-router-token")
    monkeypatch.setattr(router, "load_routes", lambda: [])
    monkeypatch.setattr(router, "fetch_codex_usage", lambda auth: {"plan": "pro", "windows": [], "credits": "5 credits", "resets": 0})
    cfg = SimpleNamespace(codex_home=tmp_path, providers=[], openai_enabled=False, listen_host="127.0.0.1", listen_port=19999,
                          base_url="http://127.0.0.1:19999/v1")

    async def run():
        app = router.build_app(cfg); runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/api/overview") as r:
                    assert r.status == 200 and r.content_type == "application/json"
                    txt = await r.text(); d = json.loads(txt)
                    assert "groups" in d and d["usage"]["plan"] == "pro"
                    assert "test-only-router-token" not in txt
                async with s.get(f"http://127.0.0.1:{port}/api/overview", headers={"cf-connecting-ip": "1.2.3.4"}) as r:
                    assert r.status == 403
        finally:
            await runner.cleanup()
    asyncio.run(run())
