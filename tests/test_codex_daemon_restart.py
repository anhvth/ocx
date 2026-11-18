import argparse
import os
import socket
from types import SimpleNamespace

import pytest

from ocx import claude_setup, cli, codex_setup, config, discovery, pi_setup


@pytest.mark.parametrize(("stale", "running", "restart"), [
    (True, True, True),
    (False, True, False),
    (True, False, False),
])
def test_start_restarts_stale_running_codex_daemon(tmp_path, monkeypatch, capsys, stale, running, restart):
    cfg = config.Config(path=None, codex_home=tmp_path, listen_host="127.0.0.1", listen_port=0,
                        provider_id="ocx", openai_enabled=True, openai_include=[], providers=[])
    catalog = tmp_path / "model-catalog.json"
    monkeypatch.setattr(codex_setup, "STATE_DIR", tmp_path)
    monkeypatch.setattr(codex_setup, "CATALOG", catalog)
    d = discovery.Discovery(discovery.OpenAIAuth("chatgpt"), [{"slug": "gpt-new"}], [])
    codex_setup.apply_codex_config(cfg, d, codex_setup.write_catalog(codex_setup.build_catalog(d)))
    catalog_mtime = catalog.stat().st_mtime_ns
    monkeypatch.setattr(discovery, "discover", lambda cfg: d)
    monkeypatch.setattr(discovery, "save_routes", lambda routes: None)
    monkeypatch.setattr(claude_setup, "apply_claude_config", lambda *a: None)
    monkeypatch.setattr(pi_setup, "apply_pi_config", lambda *a: None)
    monkeypatch.setattr(cli, "_health", lambda cfg: None)
    monkeypatch.setattr(cli, "_stop_router", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_spawn_router", lambda cfg: 123)
    commands = []

    def run(cmd, *, env, **kwargs):
        commands.append(cmd[-1])
        assert cmd[:3] == ["codex", "app-server", "daemon"]
        assert env["CODEX_HOME"] == str(tmp_path)
        status = '"running"' if running else '"not_running"'
        return SimpleNamespace(returncode=0, stdout='{"status":' + status + '}')

    monkeypatch.setattr(cli.subprocess, "run", run)

    # A short socket pathname fits even on macOS; Codex also uses a symlink here.
    with socket.socket(socket.AF_UNIX) as listener:
        socket_dir = tmp_path / "app-server-control"
        socket_dir.mkdir()
        sock = socket_dir / "app-server-control.sock"
        cwd_fd = os.open(".", os.O_RDONLY)
        try:
            os.chdir(socket_dir)
            listener.bind(sock.name)
        finally:
            os.fchdir(cwd_fd)
            os.close(cwd_fd)
        listener.listen()
        os.utime(sock, (100, 100) if stale else (2_000_000_000, 2_000_000_000))
        assert cli.cmd_start(cfg, argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert ("predates the model catalog" in out) is restart
    assert commands == (["version"] if stale else []) + (["restart"] if restart else [])
    assert catalog.stat().st_mtime_ns == catalog_mtime
