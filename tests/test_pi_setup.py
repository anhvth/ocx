import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from opencodex_desktop import config, pi_setup, router
from opencodex_desktop.discovery import Discovery, OpenAIAuth, Route


def test_pi_settings_symlink_survives_sync_and_restore(tmp_path, monkeypatch):
    home = tmp_path / "pi"
    home.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    monkeypatch.setenv("DOTFILES_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(pi_setup, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "_local").mkdir()
    target = tmp_path / "_local" / "settings.json"
    target.write_text('{"defaultModel": "gpt-test"}\n')
    link = home / "settings.json"
    link.symlink_to(target)
    cfg = SimpleNamespace(base_url="http://localhost:12345/v1", default_model="gpt-test")
    discovery = Discovery(OpenAIAuth("chatgpt"), [{"slug": "gpt-test", "context_window": 200000}], [])
    pi_setup.apply_pi_config(cfg, discovery)
    assert link.is_symlink()
    assert json.loads(target.read_text())["enabledModels"] == ["opencodex-desktop/gpt-test"]
    pi_setup.restore_pi_config(cfg)
    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"defaultModel": "gpt-test"}


def test_pi_sync_restore(tmp_path, monkeypatch):
    home = tmp_path / "pi"
    home.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(pi_setup, "TOKEN_FILE", tmp_path / "token")
    settings = {"theme": "dark", "defaultProvider": "old", "defaultModel": "old",
                "enabledModels": ["old/*"]}
    models = {"providers": {"old": {"apiKey": "secret", "models": [{"id": "old"}]}}}
    (home / "settings.json").write_text(json.dumps(settings))
    (home / "models.json").write_text(json.dumps(models))
    cfg = SimpleNamespace(base_url="http://localhost:12345/v1", default_model="vendor/model")
    route = Route(slug="vendor/model", provider="vendor", upstream_model="model", base_url="http://x",
                  wire="chat", context_window=100000, context_source="test", max_output_tokens=32000)
    d = Discovery(OpenAIAuth("chatgpt"), [{"slug": "gpt-test", "context_window": 200000}], [route])
    for _ in range(2):
        pi_setup.apply_pi_config(cfg, d)
    result = json.loads((home / "models.json").read_text())
    provider = result["providers"][pi_setup.PROVIDER]
    assert result["providers"]["old"] == models["providers"]["old"]
    assert provider["api"] == "openai-responses" and "token" in provider["apiKey"]
    assert config.router_token() not in json.dumps(provider)
    assert [m["id"] for m in provider["models"]] == ["gpt-test", "vendor/model"]
    assert provider["models"][1]["maxTokens"] == 32000
    assert provider["models"][1]["thinkingLevelMap"]["high"] == "high"
    assert json.loads((home / "settings.json").read_text())["enabledModels"] == ["opencodex-desktop/gpt-test", "opencodex-desktop/vendor/model"]
    # Re-discovery replaces the managed list; unrelated edits survive off.
    pi_setup.apply_pi_config(cfg, Discovery(d.openai, d.openai_models, []))
    result = json.loads((home / "models.json").read_text())
    assert len(result["providers"][pi_setup.PROVIDER]["models"]) == 1
    updated = json.loads((home / "settings.json").read_text())
    updated["theme"] = "light"
    (home / "settings.json").write_text(json.dumps(updated))
    pi_setup.restore_pi_config(cfg)
    assert json.loads((home / "models.json").read_text()) == models
    assert json.loads((home / "settings.json").read_text()) == {**settings, "theme": "light"}
    pi_setup.restore_pi_config(cfg)


@pytest.mark.parametrize("gateway", [True, False])
def test_gateway_openai_auth(tmp_path, monkeypatch, gateway):
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "token")
    auth = OpenAIAuth("chatgpt", upstream="http://upstream", access_token="upstream-secret", account_id="acct")
    monkeypatch.setattr(router, "read_openai_auth", lambda _: auth)
    monkeypatch.setattr(router, "load_routes", lambda: [])
    cfg = SimpleNamespace(codex_home=tmp_path, providers=[])
    r = router.Router(cfg)
    seen = {}

    async def stream(req, method, url, headers, data, label):
        seen.update(headers=headers, body=json.loads(data))
        return web.Response()

    r.stream = stream
    key = r.token if gateway else "codex-own-login"
    req = make_mocked_request("POST", "/v1/responses", headers={"Authorization": f"Bearer {key}"})
    body = {"model": "gpt-test", "input": [], "max_output_tokens": 100}
    asyncio.run(r.forward_openai(req, "v1/responses", json.dumps(body).encode()))
    assert seen["headers"]["Authorization"] == f"Bearer {'upstream-secret' if gateway else key}"
    assert seen["headers"]["ChatGPT-Account-ID"] == "acct"
    if gateway:
        assert seen["body"]["store"] is False and seen["body"]["stream"] is True
        assert "instructions" in seen["body"] and "max_output_tokens" not in seen["body"]
    else:
        assert seen["body"] == body
