import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import tomlkit

from opencodex_desktop import codex_setup, config
from opencodex_desktop.anthropic_adapter import from_messages, to_messages
from opencodex_desktop.discovery import Discovery, OpenAIAuth, Route, provider_headers
from opencodex_desktop.transforms import decode_summary, encode_summary, sanitize_request

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
    assert b["reasoning"] == {"effort": "high"} and b["store"] is False


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


def test_apply_and_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_setup, "STATE_DIR", tmp_path / "state")
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('# mine\nmodel = "old"\n\n[projects."/x"]\ntrust_level = "trusted"\n')
    cfg = config.Config(path=None, codex_home=home, listen_host="127.0.0.1", listen_port=11111,
                        provider_id="opencodex-desktop", openai_enabled=True, openai_include=[],
                        default_model=None, providers=[])
    d = Discovery(openai=OpenAIAuth(mode="chatgpt"), openai_models=[{"slug": "gpt-x"}], routes=[ROUTE])
    codex_setup.apply_codex_config(cfg, d, tmp_path / "cat.json")
    doc = tomlkit.parse((home / "config.toml").read_text())
    assert doc["model_provider"] == "opencodex-desktop" and doc["model"] == "gpt-x"
    assert doc["model_providers"]["opencodex-desktop"]["requires_openai_auth"] is True
    assert doc["projects"]["/x"]["trust_level"] == "trusted"
    assert "# mine" in (home / "config.toml").read_text()
    # the top-level keys must stay above the first table
    text = (home / "config.toml").read_text()
    assert text.index("model_provider") < text.index("[projects")

    doc["model"] = "alibaba/qwen"
    (home / "config.toml").write_text(tomlkit.dumps(doc))
    codex_setup.restore_codex_config(cfg, {"alibaba/qwen"})
    doc = tomlkit.parse((home / "config.toml").read_text())
    assert "model_provider" not in doc and "model" not in doc and "model_providers" not in doc


def test_custom_catalog_entry_keeps_instructions():
    tmpl = {"slug": "gpt-x", "model_messages": {"instructions_template": "t"}, "service_tiers": [{"id": "priority"}]}
    cat = codex_setup.build_catalog(Discovery(OpenAIAuth("chatgpt"), [tmpl], [ROUTE]))
    gpt, custom = cat["models"]
    assert gpt["service_tiers"]  # fast mode kept for GPT
    assert custom["model_messages"] and "service_tiers" not in custom
    assert custom["context_window"] == 1_000_000 and custom["auto_compact_token_limit"] == 900_000


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
    from opencodex_desktop.discovery import _detect_wire
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
    from opencodex_desktop import discovery, router as router_mod

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
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # no Codex login -> no network for GPT models
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
                    assert resp.status == 200
                    text = await resp.text()
                # compaction through the same adapter
                async with s.post(f"http://127.0.0.1:{rport}/v1/responses/compact", json=body) as resp:
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
