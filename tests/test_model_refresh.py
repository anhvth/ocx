import asyncio
import json
import urllib.error

import pytest

from ocx import codex_setup, config, discovery, pi_setup, router


def model_config(tmp_path):
    return config.Config(path=None, codex_home=tmp_path, listen_host="127.0.0.1", listen_port=0,
                         provider_id="ocx", openai_enabled=True, openai_include=[],
                         default_model=None, providers=[])


@pytest.mark.parametrize("failure", [None, 401, 503, "offline", "malformed"])
def test_live_gpt_catalog_replaces_stale_cache(tmp_path, monkeypatch, failure):
    cfg = model_config(tmp_path)
    cached = {"models": [{"slug": "gpt-old"}]}
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps(cached))
    live = [{"slug": "gpt-6.2-sol", "context_window": 1_000_000},
            {"slug": "gpt-hidden", "visibility": "hide"}, {"slug": "alibaba/qwen"}]

    def fetch(method, url, headers):
        assert method == "GET" and url.startswith(discovery.CHATGPT_BASE + "/models?")
        assert headers["authorization"] == "Bearer test"
        if failure == "offline":
            raise urllib.error.URLError("offline")
        if failure == "malformed":
            return 200, {"models": None}
        return (failure, {}) if failure else (200, {"models": live})

    monkeypatch.setattr(discovery, "http_json", fetch)
    auth = discovery.OpenAIAuth(mode="chatgpt", access_token="test")
    assert [m["slug"] for m in discovery._openai_models(cfg, auth)] == [
        "gpt-old" if failure else "gpt-6.2-sol"]
    assert json.loads(cache.read_text()) == cached  # Never overwrite Codex's own cache.
    cfg.openai_include = ["gpt-old"]
    if not failure:
        assert discovery._openai_models(cfg, auth) == []


def test_exclude_regex_filters_discovered_models_before_probing(tmp_path, monkeypatch):
    path = tmp_path / "ocx.yaml"
    path.write_text("""\
openai:
  exclude_regex: '-legacy$'
providers:
  local-vllm:
    base_url: http://localhost/v1
    exclude_regex: '(?i)(?:embed|rerank)'
""")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    cfg = config.load(path)
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-old-legacy"}, {"slug": "gpt-new"},
    ]}))
    assert [m["slug"] for m in discovery._openai_models(cfg, discovery.OpenAIAuth("apikey"))] == ["gpt-new"]

    models = ["team/Tokenizer-EMBED", "reranker", "team/Chat-New", "OtherNewModel"]
    monkeypatch.setattr(discovery, "http_json", lambda *a, **k: (200, {"data": [
        {"id": name, "max_model_len": 65_536} for name in models
    ]}))
    probes = []

    def detect_wire(provider, model, headers):
        probes.append(model)
        return "responses"

    monkeypatch.setattr(discovery, "_detect_wire", detect_wire)
    routes, ok = discovery._discover_provider(cfg.providers[0])
    assert ok and probes == models[2:]
    assert [r.slug for r in routes] == ["local-vllm/team/Chat-New", "local-vllm/OtherNewModel"]


@pytest.mark.parametrize("value", ["'['", "[foo, bar]"])
def test_invalid_exclude_regex_is_a_config_error(tmp_path, value):
    path = tmp_path / "ocx.yaml"
    path.write_text(f"openai:\n  exclude_regex: {value}\n")
    with pytest.raises(config.ConfigError, match=r"openai\.exclude_regex"):
        config.load(path)


def test_gpt_only_monitor_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "load_routes", lambda: [])
    monkeypatch.setattr(router, "router_token", lambda: "test")
    monkeypatch.setattr(router, "HEALTH_INTERVAL", 60)
    instance = router.Router(model_config(tmp_path))

    async def check():
        await instance.on_startup(None)
        assert instance.monitor is not None
        await instance.on_cleanup(None)
        await asyncio.gather(instance.monitor, return_exceptions=True)

    asyncio.run(check())


def test_monitor_syncs_gpt_changes_without_route_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "load_routes", lambda: [])
    monkeypatch.setattr(router, "router_token", lambda: "test")
    monkeypatch.setattr(discovery, "save_routes", lambda routes: None)
    catalog_path = tmp_path / "catalog.json"
    monkeypatch.setattr(codex_setup, "CATALOG", catalog_path)
    monkeypatch.setattr(codex_setup, "STATE_DIR", tmp_path)
    catalog_path.write_text(json.dumps({"models": [{"slug": "gpt-old"}]}))
    rounds = [
        [{"slug": "gpt-old"}, {"slug": "gpt-6.2-sol", "context_window": 128_000}],
        [{"slug": "gpt-6.2-sol", "context_window": 256_000}],
        [{"slug": "gpt-6.2-sol", "context_window": 512_000}],
    ]
    results = iter(rounds + [rounds[-1]])
    monkeypatch.setattr(router, "_quiet_discover", lambda cfg: (
        discovery.Discovery(discovery.OpenAIAuth("chatgpt"), next(results), []), ""))
    synced = []
    monkeypatch.setattr(pi_setup, "apply_pi_config", lambda cfg, d: synced.append(d.openai_models))
    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks > 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(router.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router.Router(model_config(tmp_path)).watch_providers())
    assert synced == rounds  # The unchanged fourth round must not rewrite Pi settings.
    assert json.loads(catalog_path.read_text())["models"] == rounds[-1]
