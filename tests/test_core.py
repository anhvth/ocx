import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import tomlkit

from ocx import claude_setup, codex_setup, config, tunnel
from ocx.anthropic_adapter import from_messages, to_messages
from ocx.discovery import Discovery, OpenAIAuth, Route, provider_headers
from ocx.transforms import decode_summary, encode_summary, sanitize_request

ROUTE = Route(slug="alibaba/qwen", provider="alibaba", upstream_model="qwen", base_url="http://x/v1",
              wire="responses", context_window=1_000_000, context_source="config")


def test_orphan_outputs_become_user_messages():
    body = {"model": "alibaba/qwen", "service_tier": "priority", "include": ["reasoning.encrypted_content"],
            "input": [
                {"type": "reasoning", "encrypted_content": "x"},
                {"type": "function_call_output", "name": "codex_apps_x", "output": "ctx"},  # no call_id
                {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "shell", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ],
            "tools": [{"type": "function", "name": "shell"}, {"type": "web_search"}],
            "reasoning": {"effort": "xhigh", "summary": "auto"}}
    b = sanitize_request(body, ROUTE)
    assert b["model"] == "qwen" and "service_tier" not in b and "include" not in b
    assert [i["type"] if "type" in i else None for i in b["input"]] == ["message", "function_call", "function_call_output"]
    assert "codex_apps_x" in b["input"][0]["content"][0]["text"]
    assert "id" not in b["input"][1]
    assert [t["type"] for t in b["tools"]] == ["function"]
    assert b["reasoning"] == {"effort": "medium"} and b["store"] is False  # unsupported xhigh -> default_effort


@pytest.mark.parametrize("tail", ["v1/responses", "v1/responses/compact"])
@pytest.mark.parametrize("gateway", [False, True])
def test_openai_drops_foreign_reasoning(tmp_path, monkeypatch, tail, gateway):
    from aiohttp.test_utils import make_mocked_request
    from ocx import router as router_mod

    monkeypatch.setattr(router_mod, "load_routes", lambda: [])
    monkeypatch.setattr(router_mod, "router_token", lambda: "router-test")
    monkeypatch.setattr(router_mod, "read_openai_auth", lambda _: OpenAIAuth(
        mode="chatgpt", access_token="openai-test", upstream="http://upstream/v1"))
    cfg = config.Config(path=None, codex_home=tmp_path, listen_host="127.0.0.1", listen_port=0,
                        provider_id="ocx", openai_enabled=True, openai_include=[],
                        providers=[])
    router = router_mod.Router(cfg)
    native = {"type": "reasoning", "id": "rs_native", "encrypted_content": "opaque", "summary": []}
    idless = {"type": "reasoning", "encrypted_content": "opaque", "summary": []}
    history = [
        {"type": "message", "id": "msg_user", "role": "user", "content": "hi"},
        {"type": "reasoning", "id": "msg_cdcf7fc1-9f45-4344-bd84-48b942576e01", "summary": []},
        native, idless,
        {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    body = {"model": "gpt-6.1-terra", "input": history, "service_tier": "priority"}
    req = make_mocked_request("POST", f"/{tail}", headers={
        "Authorization": f"Bearer {'router-test' if gateway else 'openai-test'}"})
    seen = {}

    async def stream(req, method, url, headers, data, label):
        seen.update(json.loads(data))
        return router_mod.web.json_response({"ok": True})

    monkeypatch.setattr(router, "stream", stream)
    resp = asyncio.run(router.forward_openai(req, tail, json.dumps(body).encode()))
    assert resp.status == 200
    assert seen["input"] == [history[0], native, idless, *history[4:]]
    assert seen["service_tier"] == "priority" and seen["model"] == "gpt-6.1-terra"
    # Pure GPT history must retain native reasoning, IDs and all request options.
    body["input"] = [history[0], native, idless]
    if not gateway:
        asyncio.run(router.forward_openai(req, tail, json.dumps(body).encode()))
        assert seen == body


def test_local_deepseek_v41_efforts(monkeypatch):
    from ocx import discovery

    model = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    monkeypatch.setattr(discovery, "http_json", lambda *a, **k: (200, {"data": [{"id": model, "max_model_len": 65536}]}))
    monkeypatch.setattr(discovery, "_detect_wire", lambda *a: "responses")
    provider = config.ProviderCfg(name="local-vllm", base_url="http://localhost/v1", models_auto=True)
    route = discovery._discover_provider(provider)[0][0]
    assert route.efforts == ["low", "high", "xhigh", "max"] and route.default_effort == "high"
    assert sanitize_request({"model": route.slug, "reasoning": {"effort": "medium"}}, route)["reasoning"] == {"effort": "high"}
    assert sanitize_request({"model": route.slug, "reasoning": {"effort": "high"}}, route)["reasoning"] == {"effort": "high"}


def test_effort_snaps_to_route_default():
    from ocx.messages_adapter import to_responses
    route = Route(slug="local-vllm/m", provider="local-vllm", upstream_model="m", base_url="http://x/v1",
                  wire="responses", context_window=65536, context_source="discovered",
                  efforts=["low", "high", "xhigh", "max"], default_effort="high")
    assert sanitize_request({"reasoning": {"effort": "medium"}}, route)["reasoning"] == {"effort": "high"}
    assert sanitize_request({"reasoning": {"effort": "high"}}, route)["reasoning"] == {"effort": "high"}
    assert to_responses({"effort": "medium", "messages": []}, "m", route)["reasoning"] == {"effort": "high"}
    assert to_responses({"output_config": {"effort": "high"}, "messages": []}, "m", route)["reasoning"] == {"effort": "high"}
    # Without a route (real Claude / GPT models), today's mapping is unchanged.
    assert to_responses({"effort": "medium", "messages": []}, "gpt-x")["reasoning"] == {"effort": "medium"}


def test_claude_messages_effort_snap_and_native_verbatim(tmp_path, monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from ocx import router as router_mod

    monkeypatch.setattr(router_mod, "load_routes", lambda: [])
    monkeypatch.setattr(router_mod, "router_token", lambda: "router-test")
    cfg = config.Config(path=None, codex_home=tmp_path, listen_host="127.0.0.1", listen_port=0,
                        provider_id="ocx", openai_enabled=True, openai_include=[],
                        providers=[config.ProviderCfg(
                            name="local-vllm", base_url="http://x/v1", anthropic_url="http://x/v1")])
    router = router_mod.Router(cfg)
    router.routes = {"local-vllm/m": Route(slug="local-vllm/m", provider="local-vllm", upstream_model="m",
                                           base_url="http://x/v1", wire="anthropic", context_window=65536,
                                           context_source="discovered", efforts=["low", "high", "xhigh", "max"],
                                           default_effort="high")}
    seen = {}

    async def stream(req, method, url, headers, data, label, model=None):
        seen.update(json.loads(data))
        return router_mod.web.json_response({"ok": True})

    monkeypatch.setattr(router, "stream", stream)
    req = make_mocked_request("POST", "/v1/messages")
    body = {"model": "local-vllm/m", "effort": "medium", "output_config": {"effort": "medium"},
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    asyncio.run(router.claude_messages(req, "v1/messages", json.dumps(body).encode()))
    assert seen["effort"] == "high" and seen["output_config"]["effort"] == "high"
    assert seen["max_tokens"] == 16 and seen["messages"] == body["messages"]  # rest forwarded untouched
    seen.clear()
    native = {"model": "claude-sonnet-5-5", "effort": "medium", "messages": body["messages"]}
    native_req = make_mocked_request("POST", "/v1/messages", headers={"Authorization": "Bearer claude-client-login"})
    asyncio.run(router.claude_messages(native_req, "v1/messages", json.dumps(native).encode()))
    assert seen == native  # real Anthropic gets the body verbatim


def test_summary_envelope_roundtrip():
    assert decode_summary(encode_summary("héllo")) == "héllo"
    assert decode_summary("gAAAA-opaque") is None


def test_config_parsing(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("MY_KEY", "sk-test")
    p = tmp_path / "c.yaml"
    p.write_text("listen: 127.0.0.1:12345\nproviders:\n  a:\n    base_url: http://h/v1/\n    api_key_env: MY_KEY\n"
                 "    models:\n      - m1\n      - m2: {context_window: 5000}\n  b:\n    base_url: http://k/v1\n")
    c = config.load(p)
    assert c.listen_port == 12345 and c.base_url == "http://127.0.0.1:12345/v1"
    a, b = c.providers
    assert a.base_url == "http://h/v1" and a.api_key == "sk-test"
    assert [m.id for m in a.models] == ["m1", "m2"] and a.models[1].context_window == 5000
    assert b.models_auto


def test_config_errors(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("providers:\n  a:\n    base_url: http://h\n    api_key_env: DEFINITELY_UNSET_VAR\n")
    with pytest.raises(config.ConfigError):
        config.load(p)


@pytest.mark.parametrize("model", [None, "old", "alibaba/qwen"])
def test_apply_and_restore(tmp_path, monkeypatch, model):
    monkeypatch.setattr(codex_setup, "STATE_DIR", tmp_path / "state")
    home = tmp_path / "codex"
    home.mkdir()
    path = home / "config.toml"
    model_line = f'model = "{model}"\n' if model else ""
    path.write_text(f'# mine\n{model_line}\n[projects."/x"]\ntrust_level = "trusted"\n')
    cfg = config.Config(path=None, codex_home=home, listen_host="127.0.0.1", listen_port=11111,
                        provider_id="ocx", openai_enabled=True, openai_include=[], providers=[])
    d = Discovery(openai=OpenAIAuth(mode="chatgpt"), openai_models=[{"slug": "gpt-x"}], routes=[ROUTE])
    codex_setup.apply_codex_config(cfg, d, tmp_path / "state" / "model-catalog.json")
    doc = tomlkit.parse(path.read_text())
    assert doc["model_provider"] == "ocx" and doc.get("model") == model
    assert doc["model_providers"]["ocx"]["requires_openai_auth"] is True
    assert doc["projects"]["/x"]["trust_level"] == "trusted"
    assert "# mine" in (home / "config.toml").read_text()
    # the top-level keys must stay above the first table
    text = (home / "config.toml").read_text()
    assert text.index("model_provider") < text.index("[projects")

    codex_setup.restore_codex_config(cfg)
    doc = tomlkit.parse(path.read_text())
    assert not {"model_provider", "model_providers", "model_catalog_json"}.intersection(doc)
    assert doc.get("model") == model


def test_claude_unmanaged_symlink_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTFILES_DIR", str(tmp_path / "repo"))
    target = tmp_path / "tracked.json"
    target.write_text("{}\n")
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    claude_setup._write(link, {"env": {"secret": "local"}})
    assert not link.is_symlink()
    assert target.read_text() == "{}\n"
    assert link.stat().st_mode & 0o777 == 0o600


def test_claude_apply_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTFILES_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(claude_setup, "CLAUDE_HOME", tmp_path / "claude")
    monkeypatch.setattr(claude_setup, "SETTINGS", tmp_path / "claude" / "settings.json")
    monkeypatch.setattr(claude_setup, "DESKTOP", tmp_path / "desktop")
    for name in ("PIDFILE", "URLFILE", "LOGFILE"):
        monkeypatch.setattr(tunnel, name, tmp_path / name)
    (tmp_path / "claude").mkdir()
    (tmp_path / "_local").mkdir()
    user_settings = {"theme": "dark", "model": "claude-sonnet-5-5",
                     "env": {"FOO": "1", "ANTHROPIC_MODEL": "claude-opus-5-5"},
                     "modelPicker": {"options": [{"model": "mine"}]}}
    (tmp_path / "_local" / "real.json").write_text(json.dumps(user_settings))
    (tmp_path / "claude" / "settings.json").symlink_to(tmp_path / "_local" / "real.json")
    (tmp_path / "desktop" / "configLibrary").mkdir(parents=True)
    (tmp_path / "desktop" / "configLibrary" / "old.json").write_text("{}")
    (tmp_path / "desktop" / "configLibrary" / "_meta.json").write_text(json.dumps(
        {"appliedId": "old", "entries": [{"id": "old", "name": "Old"}]}))
    other = config.ProviderCfg(name="other", base_url="http://y/v1")
    cfg = config.Config(path=None, codex_home=tmp_path, listen_host="127.0.0.1", listen_port=11111,
                        provider_id="ocx", openai_enabled=True, openai_include=[],
                        providers=[config.ProviderCfg(name="alibaba", base_url="http://x/v1", anthropic_url="http://x/a"),
                                   other])
    other_route = Route(slug="other/m", provider="other", upstream_model="m", base_url="http://y/v1",
                        wire="chat", context_window=100_000, context_source="config")
    route_low = Route(slug="alibaba/qwen", provider="alibaba", upstream_model="qwen", base_url="http://x/v1",
                      wire="responses", context_window=1_000_000, context_source="config",
                      efforts=["low", "high"], default_effort="low")
    d = Discovery(openai=OpenAIAuth(mode="chatgpt"), openai_models=[{"slug": "gpt-x", "context_window": 272_000}],
                  routes=[route_low, other_route])
    # A stale case-variant key of the same slug must not survive as a duplicate.
    real = tmp_path / "_local" / "real.json"
    real.write_text(json.dumps({**json.loads(real.read_text()),
                                "modelSettings": {"Alibaba/Qwen": {"autoCompactWindow": 900_000, "_opencodexDesktop": True}}}))
    for _ in range(2):  # idempotent
        claude_setup.apply_claude_config(cfg, d)
    path = tmp_path / "claude" / "settings.json"
    assert path.is_symlink() and path.stat().st_mode & 0o777 == 0o600
    s = json.loads(path.read_text())
    assert s["theme"] == "dark" and s["env"]["FOO"] == "1"
    assert s["model"] == user_settings["model"]
    assert s["env"]["ANTHROPIC_MODEL"] == user_settings["env"]["ANTHROPIC_MODEL"]
    assert s["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11111"
    assert s["env"]["ANTHROPIC_CUSTOM_HEADERS"] == f"x-ocx-token: {config.router_token()}"
    assert [o["model"] for o in s["modelPicker"]["options"]] == ["mine", "gpt-x", "alibaba/qwen[1m]"]
    assert s["modelSettings"]["gpt-x"]["autoCompactWindow"] == 244_800
    assert s["modelSettings"]["alibaba/qwen"]["effortLevel"] == "low"
    assert [k for k in s["modelSettings"] if k.lower() == "alibaba/qwen"] == ["alibaba/qwen"]
    assert "effortLevel" not in s["modelSettings"]["gpt-x"]
    lib = tmp_path / "desktop" / "configLibrary"
    meta = json.loads((lib / "_meta.json").read_text())
    assert meta["appliedId"] == claude_setup.PROFILE_ID and meta["previousAppliedId"] == "old"
    prof = json.loads((lib / f"{claude_setup.PROFILE_ID}.json").read_text())
    names = [m["name"] for m in prof["inferenceModels"]]
    assert names == ["gpt-x", "alibaba/qwen"]  # no Anthropic API key: no claude-* rows
    assert prof["inferenceGatewayApiKey"] == config.router_token()

    claude_setup.restore_claude_config(cfg)
    assert json.loads(path.read_text()) == user_settings
    meta = json.loads((lib / "_meta.json").read_text())
    assert meta == {"appliedId": "old", "entries": [{"id": "old", "name": "Old"}]}
    assert not (lib / f"{claude_setup.PROFILE_ID}.json").exists()


def test_custom_catalog_entry_keeps_instructions():
    tmpl = {"slug": "gpt-x", "model_messages": {"instructions_template": "t"}, "service_tiers": [{"id": "priority"}]}
    cat = codex_setup.build_catalog(Discovery(OpenAIAuth("chatgpt"), [tmpl], [ROUTE]))
    gpt, custom = cat["models"]
    assert gpt["service_tiers"]  # fast mode kept for GPT
    assert custom["model_messages"] and "service_tiers" not in custom
    assert custom["context_window"] == 1_000_000 and custom["auto_compact_token_limit"] == 900_000


def test_catalog_keeps_native_automatic_default_above_custom_models():
    gpt = [{"slug": "gpt-other", "priority": 2}, {"slug": "gpt-default", "priority": 1}]
    catalog = codex_setup.build_catalog(Discovery(OpenAIAuth("chatgpt"), gpt, [ROUTE]))
    ordered = sorted(catalog["models"], key=lambda m: m["priority"])
    assert [m["slug"] for m in ordered] == ["gpt-default", ROUTE.slug, "gpt-other"]
    assert gpt[1]["priority"] == 1


# ---------------------------------------------------------------- anthropic

ANTH_ROUTE = Route(slug="anthropic/claude-test", provider="anthropic", upstream_model="claude-test",
                   base_url="https://api.anthropic.com/v1", wire="anthropic", context_window=200_000,
                   context_source="anthropic default", max_output_tokens=64_000)


def test_anthropic_config_synthesis(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    p = tmp_path / "c.yaml"
    # no block, no env key -> no anthropic provider
    p.write_text("listen: 127.0.0.1:12345\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.load(p).providers == []
    # env key alone -> built-in provider, models auto
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    a, = config.load(p).providers
    assert a.name == "anthropic" and a.wire_api == "anthropic" and a.models_auto
    assert a.base_url == "https://api.anthropic.com/v1" and a.api_key == "sk-ant-test"
    # explicit block wins; enabled: false disables
    p.write_text("anthropic: {enabled: false}\n")
    assert config.load(p).providers == []
    p.write_text("anthropic: {api_key: sk-ant-x, include: [sonnet]}\nproviders:\n  anthropic:\n    base_url: http://gw/v1\n")
    a, = config.load(p).providers  # manual providers.anthropic is not duplicated
    assert a.base_url == "http://gw/v1" and a.api_key is None
    # anthropic.com base_url auto-detects the wire
    p.write_text("providers:\n  claude:\n    base_url: https://api.anthropic.com/v1\n    api_key: k\n")
    assert config.load(p).providers[0].wire_api == "anthropic"


def test_anthropic_headers_and_wire():
    p = config.ProviderCfg(name="anthropic", base_url="https://api.anthropic.com/v1",
                           api_key="sk-ant-x", wire_api="anthropic")
    h = provider_headers(p)
    assert h["x-api-key"] == "sk-ant-x" and h["anthropic-version"] == "2023-06-01"
    assert "authorization" not in h
    from ocx.discovery import _detect_wire
    p.wire_api = None
    assert _detect_wire(p, "claude-x", {}) == "anthropic"  # no probe needed


def test_anthropic_to_messages():
    body = {
        "model": "claude-test", "instructions": "be helpful", "max_output_tokens": 32000,
        "reasoning": {"effort": "high"}, "temperature": 0.5,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev note"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "calling"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd": "ls"}'},
            {"type": "custom_tool_call", "call_id": "c2", "name": "apply_patch", "input": "*** patch"},
            {"type": "function_call_output", "call_id": "c1", "output": "file.txt"},
            {"type": "custom_tool_call_output", "call_id": "c2", "output": "ok"},
        ],
        "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}},
                  {"type": "custom", "name": "apply_patch"}],
    }
    payload, custom = to_messages(body, ANTH_ROUTE)
    assert custom == {"apply_patch"}
    assert payload["system"] == "be helpful\n\ndev note"
    assert payload["max_tokens"] == 32000 and payload["stream"] is False
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 30976}  # capped at max_tokens-1024
    assert "temperature" not in payload  # incompatible with thinking
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "user"]  # consecutive same-role items merged
    asst = payload["messages"][1]["content"]
    assert [b["type"] for b in asst] == ["text", "tool_use", "tool_use"]
    assert asst[1]["input"] == {"cmd": "ls"} and asst[2]["input"] == {"input": "*** patch"}
    user2 = payload["messages"][2]["content"]
    assert [b["type"] for b in user2] == ["tool_result", "tool_result"]
    assert user2[0]["tool_use_id"] == "c1" and user2[0]["content"] == "file.txt"
    assert {t["name"] for t in payload["tools"]} == {"shell", "apply_patch"}
    assert payload["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


def test_anthropic_thinking_edges():
    body = {"model": "claude-test", "input": ["hi"], "reasoning": {"effort": "none"}, "temperature": 0.2}
    payload, _ = to_messages(body, ANTH_ROUTE)
    assert "thinking" not in payload and payload["temperature"] == 0.2
    # tiny max_tokens -> budget cannot fit, thinking off
    body = {"model": "claude-test", "input": ["hi"], "reasoning": {"effort": "high"}, "max_output_tokens": 1500}
    payload, _ = to_messages(body, ANTH_ROUTE)
    assert "thinking" not in payload
    # forced tool use is downgraded when thinking is on; "none" drops tools
    body = {"model": "claude-test", "input": ["hi"], "reasoning": {"effort": "low"}, "tool_choice": "required",
            "tools": [{"type": "function", "name": "f"}]}
    payload, _ = to_messages(body, ANTH_ROUTE)
    assert payload["thinking"]["budget_tokens"] == 4096 and "tool_choice" not in payload
    body["tool_choice"] = "none"
    payload, _ = to_messages(body, ANTH_ROUTE)
    assert "tools" not in payload


def test_anthropic_from_messages():
    resp = {"content": [{"type": "thinking", "thinking": "hmm"},
                        {"type": "text", "text": "done"},
                        {"type": "tool_use", "id": "tu1", "name": "shell", "input": {"cmd": "ls"}},
                        {"type": "tool_use", "id": "tu2", "name": "apply_patch", "input": {"input": "p"}}],
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}}
    items, usage = from_messages(resp, {"apply_patch"})
    assert [i["type"] for i in items] == ["message", "function_call", "custom_tool_call"]
    assert items[0]["content"][0]["text"] == "done"
    assert items[1]["call_id"] == "tu1" and json.loads(items[1]["arguments"]) == {"cmd": "ls"}
    assert items[2]["input"] == "p"
    assert usage["input_tokens"] == 115 and usage["total_tokens"] == 135
    assert usage["input_tokens_details"]["cached_tokens"] == 100


def test_anthropic_end_to_end(tmp_path, monkeypatch):
    """start-style discovery + a Responses request through the router against a mock Anthropic API."""
    from ocx import discovery, router as router_mod

    seen = {}

    class Mock(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            assert self.path == "/v1/models"
            assert self.headers["x-api-key"] == "sk-ant-test" and self.headers["anthropic-version"]
            self._send({"data": [{"id": "claude-test", "display_name": "Claude Test",
                                  "max_tokens": 8192, "max_input_tokens": 200000}]})

        def do_POST(self):
            assert self.path == "/v1/messages"
            seen["payloads"] = seen.get("payloads", []) + [json.loads(self.rfile.read(int(self.headers["content-length"])))]
            self._send({"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
                        "content": [{"type": "text", "text": "hello from claude"},
                                    {"type": "tool_use", "id": "tu1", "name": "shell", "input": {"cmd": "ls"}}],
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 5, "output_tokens": 7}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        monkeypatch.setattr(discovery, "STATE_DIR", tmp_path)
        monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "token")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # no Codex login -> no network for GPT models
        tok = {"x-opencodex-token": config.router_token()}
        p = tmp_path / "c.yaml"
        p.write_text(f"anthropic:\n  api_key: sk-ant-test\n  base_url: http://127.0.0.1:{port}/v1\n")
        cfg = config.load(p)
        d = discovery.discover(cfg)
        r, = d.routes
        assert r.slug == "anthropic/claude-test" and r.wire == "anthropic"
        assert r.context_window == 200000 and r.max_output_tokens == 8192
        assert r.display_name == "Claude Test" and r.input_modalities == ["text", "image"]
        discovery.save_routes(d.routes)

        async def run():
            app = router_mod.build_app(cfg)
            runner = router_mod.web.AppRunner(app)
            await runner.setup()
            site = router_mod.web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            rport = site._server.sockets[0].getsockname()[1]
            body = {"model": "anthropic/claude-test", "stream": True, "instructions": "sys",
                    "reasoning": {"effort": "medium"},
                    "input": [{"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": "hi"}]}],
                    "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}]}
            async with router_mod.aiohttp.ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{rport}/v1/responses", json=body) as resp:
                    assert resp.status == 401  # no router token
                async with s.get(f"http://127.0.0.1:{rport}/") as resp:  # browser page, no token
                    assert resp.status == 200 and "anthropic/claude-test" in await resp.text()
                async with s.get(f"http://127.0.0.1:{rport}/", headers={"cf-connecting-ip": "1.2.3.4"}) as resp:
                    assert resp.status == 403
                async with s.post(f"http://127.0.0.1:{rport}/v1/responses", json=body, headers=tok) as resp:
                    assert resp.status == 200
                    text = await resp.text()
                # compaction through the same adapter
                # Claude client (Anthropic Messages) asking for the same model: native passthrough
                mbody = {"model": "anthropic/claude-test", "max_tokens": 50,
                         "messages": [{"role": "user", "content": "hi"}]}
                async with s.post(f"http://127.0.0.1:{rport}/v1/messages", json=mbody,
                                  headers={"anthropic-version": "2023-06-01"}) as resp:
                    assert resp.status == 401
                async with s.post(f"http://127.0.0.1:{rport}/v1/messages", json=mbody,
                                  headers={"anthropic-version": "2023-06-01", "x-api-key": tok["x-opencodex-token"]}) as resp:
                    assert resp.status == 200, await resp.text()
                    assert seen["payloads"][-1]["model"] == "claude-test"
                async with s.get(f"http://127.0.0.1:{rport}/v1/models",
                                 headers={"anthropic-version": "2023-06-01", **tok}) as resp:
                    ids = [m["id"] for m in (await resp.json())["data"]]
                    assert "anthropic/claude-test" in ids and "claude-opus-5-5" in ids
                async with s.post(f"http://127.0.0.1:{rport}/v1/responses/compact", json=body, headers=tok) as resp:
                    assert resp.status == 200
                    compact = await resp.json()
            await runner.cleanup()
            return text, compact

        text, compact = asyncio.run(run())
        assert "response.completed" in text and "hello from claude" in text
        assert '"name": "shell"' in text and "function_call" in text
        payload = seen["payloads"][0]  # first request; the second is compaction (no tools)
        assert payload["model"] == "claude-test" and payload["system"] == "sys"
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8192 - 1024}
        assert payload["tools"][0]["name"] == "shell"
        assert any("hello from claude" in m["content"][0]["text"] for m in compact["output"])
    finally:
        srv.shutdown()


def test_codex_internal_items_become_text():
    body = {"model": "alibaba/qwen", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "agent_message", "id": "am_1", "author": "worker-1",
         "content": [{"type": "output_text", "text": "subtask done"}]},
        {"type": "web_search_call", "id": "ws_1", "status": "completed"},
    ]}
    inp = sanitize_request(body, ROUTE)["input"]
    assert [i.get("type") for i in inp] == ["message", "message"]
    assert inp[1]["role"] == "user" and "subtask done" in inp[1]["content"][0]["text"]
    assert "worker-1" in inp[1]["content"][0]["text"]


def test_messages_to_responses_and_back():
    from ocx.messages_adapter import Translator, to_responses
    body = {"model": "gpt-x", "system": [{"type": "text", "text": "be brief"}], "stream": True,
            "thinking": {"type": "enabled", "budget_tokens": 20000},
            "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}},
                      {"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": "list files"},
                         {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "s"},
                                                           {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                       "content": [{"type": "text", "text": "a.txt"}]}]}]}
    r = to_responses(body, "gpt-x")
    assert r["instructions"] == "be brief" and r["reasoning"] == {"effort": "high"} and r["store"] is False
    assert [t["name"] for t in r["tools"]] == ["Bash"]  # server tools have no input_schema
    assert [i.get("type", "message") for i in r["input"]] == ["message", "function_call", "function_call_output"]
    assert r["input"][1]["arguments"] == '{"cmd": "ls"}' and r["input"][2]["output"] == "a.txt"

    tr = Translator("gpt-x")
    out = tr.start()
    for ev in [{"type": "response.output_text.delta", "output_index": 0, "delta": "Hel"},
               {"type": "response.output_text.delta", "output_index": 0, "delta": "lo"},
               {"type": "response.output_item.done", "output_index": 0},
               {"type": "response.output_item.added", "output_index": 1,
                "item": {"type": "function_call", "call_id": "c1", "name": "Bash"}},
               {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"cmd":'},
               {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"pwd"}'},
               {"type": "response.output_item.done", "output_index": 1},
               {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 3}}}]:
        out += tr.feed(ev)
    out += tr.finish()
    events = [line[7:] for line in out.decode().splitlines() if line.startswith("event: ")]
    assert events == ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                      "content_block_stop", "content_block_start", "content_block_delta", "content_block_delta",
                      "content_block_stop", "message_delta", "message_stop"]
    msg = tr.message()
    assert msg["content"][0] == {"type": "text", "text": "Hello"}
    assert msg["content"][1]["input"] == {"cmd": "pwd"} and msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_fold_system_messages():
    from ocx.messages_adapter import fold_system_messages
    msgs = [{"role": "system", "content": "first"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
            {"role": "system", "content": [{"type": "text", "text": "tools changed"}]}]
    out = fold_system_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert "first" in out[0]["content"][0]["text"] and out[0]["content"][1]["text"] == "hi"
    assert out[2]["content"][0]["type"] == "tool_result" and "tools changed" in out[2]["content"][1]["text"]


def test_context_caps(tmp_path, monkeypatch):
    from ocx import discovery
    monkeypatch.setattr(discovery, "http_json", lambda *a, **k: (503, {}))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-a", "context_window": 500000, "auto_compact_token_limit": 450000},
        {"slug": "gpt-b", "context_window": 272000},
        {"slug": "gpt-c", "context_window": 128000},
        {"slug": "gpt-skip", "context_window": 500000}]}))
    p = tmp_path / "c.yaml"
    p.write_text(f"codex_home: [{home}]\nmax_context_window: 300000\n"
                 "openai:\n  include: [gpt-a, gpt-b, gpt-c]\n  context_window: {gpt-b: 256000}\n")
    cfg = config.load(p)
    ms = discovery._openai_models(cfg, OpenAIAuth(mode="chatgpt"))
    assert [(m["slug"], m["context_window"]) for m in ms] == [("gpt-a", 300000), ("gpt-b", 256000), ("gpt-c", 128000)]
    assert ms[0]["max_context_window"] == 300000 and ms[0]["auto_compact_token_limit"] == 270000
