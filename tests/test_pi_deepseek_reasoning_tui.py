"""Run: OCX_TMUX_RED_TEST=1 uv run pytest -q -s tests/test_pi_deepseek_reasoning_tui.py."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

from ocx import config, discovery, pi_setup


@pytest.mark.skipif(os.environ.get("OCX_TMUX_RED_TEST") != "1", reason="opt-in real Pi/tmux test")
def test_local_deepseek_effort_picker_offers_high(monkeypatch):
    """Select local DeepSeek, /effort + Enter: high must be a choice.

    No persistence/restart claim; all config and session state is disposable.
    """
    assert shutil.which("tmux") and shutil.which("pi"), "Install tmux and Pi first"
    extension = Path(os.environ.get(
        "PI_EFFORT_EXTENSION", str(Path.home() / ".pi/agent/npm/node_modules/pi-effort/index.ts")))
    assert extension.is_file(), "Set PI_EFFORT_EXTENSION to the installed pi-effort/index.ts"
    scratch = Path(tempfile.mkdtemp(prefix="ocx-pi-deepseek-red-"))
    print(f"Evidence: {scratch}")
    home = scratch / "agent"
    home.mkdir()
    model = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    monkeypatch.setattr(discovery, "http_json", lambda *a, **k: (
        200, {"data": [{"id": model, "max_model_len": 65536}]}))
    provider = config.ProviderCfg(name="local-vllm", base_url="http://127.0.0.1:1/v1",
                                  wire_api="responses", models_auto=True)
    routes, ok = discovery._discover_provider(provider)
    assert ok and len(routes) == 1
    models = pi_setup.build_models(discovery.Discovery(discovery.OpenAIAuth("none"), [], routes))
    # Use the real discovery -> Pi conversion, not hand-written effort metadata.
    (home / "models.json").write_text(json.dumps({"providers": {"ocx": {
        "baseUrl": "http://127.0.0.1:1/v1", "api": "openai-responses",
        "apiKey": "fixture-not-a-secret", "models": models}}}, indent=2))
    (home / "settings.json").write_text(json.dumps({
        "defaultProvider": "ocx", "defaultModel": routes[0].slug,
        "quietStartup": True, "enabledModels": [f"ocx/{routes[0].slug}"]}))
    socket = "ocx-pi-red-" + uuid.uuid4().hex[:10]
    session = "code-agent-codex-pi-deepseek-red"

    def tmux(*args, check=True):
        return subprocess.run(["tmux", "-L", socket, *args], check=check,
                              text=True, capture_output=True).stdout

    def wait_for(predicate, state):
        deadline = time.monotonic() + 30
        pane = ""
        while time.monotonic() < deadline:
            pane = tmux("capture-pane", "-pt", session)
            if predicate(pane):
                return pane
            time.sleep(0.1)
        pytest.fail(f"SETUP FAILURE waiting for {state}; not a red regression.\n{pane}")

    def text(value):
        tmux("send-keys", "-t", session, "-l", value)
        tmux("send-keys", "-t", session, "Enter")

    try:
        tmux("-f", "/dev/null", "new-session", "-d", "-s", session,
             "-x", "140", "-y", "40", "-c", str(scratch), "/bin/sh")
        # Run the real Pi binary; only the relevant installed extension is loaded.
        command = ["env", f"PI_CODING_AGENT_DIR={home}", "PI_TELEMETRY=0",
                   "TERM=xterm-256color", shutil.which("pi"), "--offline",
                   "--no-extensions", "--extension", str(extension), "--no-mcp",
                   "--no-skills", "--no-prompt-templates", "--no-context-files",
                   "--no-themes", "--no-tools", "--no-approve",
                   "--session-dir", str(scratch / "sessions")]
        text(shlex.join(command))
        wait_for(lambda p: model in p and "think:" in p, "Pi startup footer")
        text("/model")
        wait_for(lambda p: "Enter to select · Ctrl+S to set as default" in p, "model picker")
        tmux("send-keys", "-t", session, "-l", model)
        wait_for(lambda p: f"Model Name: local-vllm/{model}" in p, "filtered DeepSeek model")
        tmux("send-keys", "-t", session, "Enter")
        selected = wait_for(lambda p: "Enter to select · Ctrl+S to set as default" not in p and model in p,
                            "selected DeepSeek footer")
        (scratch / "selected-model.log").write_text(selected)
        text("/effort")
        pane = wait_for(lambda p: "Effort (current:" in p, "effort picker")
        (scratch / "effort-picker.log").write_text(pane)
        # Anchor to option rows: xhigh and footer/status text must not match high.
        actual = re.findall(r"^\s*(?:[→›❯>]\s*)?(min|low|medium|high|xhigh|max|off|default)\s*$",
                            pane, re.MULTILINE)
        assert actual, f"SETUP FAILURE: no effort option rows parsed.\n{pane}"
        assert "high" in actual, (
            f"Expected: high available for local DeepSeek. Actual effort options: {actual}.\n{pane}")
    finally:
        transcript = tmux("capture-pane", "-pt", session, "-S", "-150", check=False)
        (scratch / "pane.log").write_text(transcript)
        tmux("kill-server", check=False)
