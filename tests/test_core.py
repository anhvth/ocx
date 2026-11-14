from pathlib import Path

import pytest
import tomlkit

from opencodex_desktop import codex_setup, config
from opencodex_desktop.discovery import Discovery, OpenAIAuth, Route
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
