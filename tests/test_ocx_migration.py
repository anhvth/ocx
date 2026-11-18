import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import tomlkit
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from ocx import claude_setup, codex_setup, config, pi_setup, router
from ocx.discovery import Discovery, OpenAIAuth


def test_legacy_config_and_state_are_read_without_moving_the_token(tmp_path):
    legacy_config = tmp_path / "config/opencodex-desktop"
    legacy_config.mkdir(parents=True)
    (legacy_config / "config.yaml").write_text("listen: 127.0.0.1:19001\n")
    legacy_state = tmp_path / "state/opencodex-desktop"
    legacy_state.mkdir(parents=True)
    (legacy_state / "token").write_text("test-old-token\n")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    env.pop("OCX_CONFIG", None)
    env.pop("OPENCODEX_DESKTOP_CONFIG", None)
    subprocess.run([sys.executable, "-c", f"""
from {config.__package__} import config
cfg = config.load()
assert cfg.listen_port == 19001 and cfg.provider_id == "ocx"
assert config.STATE_DIR.name == "opencodex-desktop"
assert config.router_token() == "test-old-token"
"""], env=env, check=True)
    assert not (tmp_path / "state/ocx").exists()


def test_codex_keeps_legacy_provider_for_resumed_threads(tmp_path, monkeypatch):
    legacy_state = tmp_path / "opencodex-desktop"
    monkeypatch.setattr(codex_setup, "LEGACY_STATE_DIR", legacy_state)
    catalog = legacy_state / "model-catalog.json"
    path = tmp_path / "config.toml"
    path.write_text('model_provider = "opencodex-desktop"\n'
                    '[model_providers.opencodex-desktop]\n'
                    'base_url = "http://127.0.0.1:11111/v1"\n'
                    '[model_providers.unrelated]\n'
                    'name = "keep this"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    yaml = tmp_path / "config.yaml"
    yaml.write_text("listen: 127.0.0.1:19001\n")
    cfg = config.load(yaml)
    d = Discovery(OpenAIAuth("chatgpt"), [{"slug": "gpt-test"}], [])
    codex_setup.apply_codex_config(cfg, d, catalog)
    written = path.read_text()
    codex_setup.apply_codex_config(cfg, d, catalog)
    assert path.read_text() == written
    doc = tomlkit.parse(written)
    assert doc["model_provider"] == "ocx"
    assert doc["model_providers"]["ocx"] == doc["model_providers"]["opencodex-desktop"]
    assert doc["model_providers"]["opencodex-desktop"]["base_url"] == cfg.base_url
    codex_setup.restore_codex_config(cfg)
    doc = tomlkit.parse(path.read_text())
    assert "model_provider" not in doc and "model_catalog_json" not in doc
    assert set(doc["model_providers"]) == {"unrelated"}


def test_pi_alias_and_original_backup_survive_rename(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    monkeypatch.setattr(pi_setup, "router_token", lambda: "test")
    models, settings = (tmp_path / n for n in ("models.json", "settings.json"))
    models.write_text(json.dumps({"providers": {"opencodex-desktop": {"baseUrl": "old-managed"},
                                                 "other": {"baseUrl": "keep"}}}))
    settings.write_text(json.dumps({"defaultProvider": "opencodex-desktop", "theme": "dark"}))
    legacy_backup = tmp_path / "opencodex-desktop-backup.json"
    legacy_backup.write_text(json.dumps({"provider": None, "settings": {"defaultProvider": "other"}}))
    cfg = SimpleNamespace(base_url="http://127.0.0.1:19001/v1", default_model=None)
    d = Discovery(OpenAIAuth("chatgpt"), [{"slug": "gpt-test"}], [])
    pi_setup.apply_pi_config(cfg, d)
    providers = json.loads(models.read_text())["providers"]
    assert providers["ocx"] == providers["opencodex-desktop"]
    assert json.loads(settings.read_text())["enabledModels"] == ["ocx/gpt-test"]
    pi_setup.restore_pi_config(cfg)
    assert json.loads(models.read_text()) == {"providers": {"other": {"baseUrl": "keep"}}}
    assert json.loads(settings.read_text()) == {"defaultProvider": "other", "theme": "dark"}
    assert not legacy_backup.exists() and not (tmp_path / "ocx-backup.json").exists()


def test_claude_legacy_generated_settings_are_removed():
    settings = {
        "env": {"ANTHROPIC_CUSTOM_HEADERS": "x-opencodex-token: test", "ANTHROPIC_BASE_URL": "http://old", "OTHER": "keep"},
        "modelPicker": {"options": [{"description": "128k context, via opencodex-desktop"}, {"model": "keep"}]},
        "modelSettings": {"generated": {"_opencodexDesktop": True}, "keep": {"autoCompactWindow": 10}},
    }
    claude_setup._strip_ours(settings)
    assert settings == {
        "env": {"OTHER": "keep"},
        "modelPicker": {"options": [{"model": "keep"}]},
        "modelSettings": {"keep": {"autoCompactWindow": 10}},
    }


@pytest.mark.parametrize("header", ["x-ocx-token", "x-opencodex-token", "x-api-key"])
def test_router_accepts_local_token_but_does_not_send_it_upstream(tmp_path, monkeypatch, header):
    monkeypatch.setattr(router, "router_token", lambda: "local-token")
    monkeypatch.setattr(router, "read_openai_auth", lambda _: OpenAIAuth("chatgpt", "http://upstream", access_token="test"))
    r = router.Router(SimpleNamespace(codex_home=tmp_path, providers=[]))
    req = make_mocked_request("POST", "/v1/responses", headers={header: r.token})

    async def stream(req, method, url, headers, data, label):
        assert headers["Authorization"] == "Bearer test"
        assert header not in {k.lower() for k in headers}
        return web.Response()

    monkeypatch.setattr(r, "stream", stream)
    assert r.authorized(req, claude=False)
    assert asyncio.run(r.forward_openai(req, "v1/responses", b'{"model": "gpt-test"}')).status == 200
