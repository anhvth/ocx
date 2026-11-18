import argparse
import stat
import sys
from types import SimpleNamespace

from ocx import cli, config


def test_setup_writes_config_saves_key_and_starts(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "config.yaml"
    key_path = tmp_path / "keys" / "alibaba" / "token"
    monkeypatch.setattr(config, "DEFAULT_CONFIG", cfg_path)
    monkeypatch.setattr(config, "LEGACY_CONFIG", tmp_path / "missing-legacy.yaml")
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", cfg_path)  # cli imported the constant by value
    monkeypatch.delenv("OCX_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODEX_DESKTOP_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # keep load() away from the real config
    monkeypatch.setattr(cli, "_check_update", lambda: None)
    monkeypatch.setattr(cli, "EXAMPLE", tmp_path / "example.yaml")
    (tmp_path / "example.yaml").write_text(
        f"codex_home: [{tmp_path}]\nproviders:\n  alibaba:\n"
        f"    base_url: https://example.test/v1\n    api_key_file: {key_path}\n    models: auto\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "sk-secret-abcd")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    from ocx import discovery
    monkeypatch.setattr(discovery, "http_json", lambda *a, **k: (200, {"data": []}))

    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "1/4 Config" in out and "key accepted" in out and "Run `ocx start` when ready" in out
    assert key_path.read_text().strip() == "sk-secret-abcd"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert "sk-secret" not in out and "abcd" in out  # only last 4 shown


def test_setup_requires_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "_check_update", lambda: None)
    assert cli.main(["setup"]) == 1
    assert "interactive" in capsys.readouterr().out


def test_setup_enter_keeps_existing_key_and_runs_start(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "config.yaml"
    key_path = tmp_path / "token"
    key_path.write_text("existing-key\n")
    cfg_path.write_text(f"codex_home: [{tmp_path}]\nproviders:\n  alibaba:\n"
                        f"    base_url: https://example.test/v1\n    api_key_file: {key_path}\n    models: auto\n")
    monkeypatch.setattr(cli, "_check_update", lambda: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    from ocx import discovery
    monkeypatch.setattr(discovery, "http_json", lambda *a, **k: (200, {"data": []}))
    started = []
    monkeypatch.setattr(cli, "cmd_start", lambda cfg, args: started.append(cfg.path) or 0)

    assert cli.main(["--config", str(cfg_path), "setup"]) == 0
    assert key_path.read_text() == "existing-key\n"
    assert started == [cfg_path]
    assert "key file" in capsys.readouterr().out
