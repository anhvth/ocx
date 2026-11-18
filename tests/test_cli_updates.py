import io
import json

import pytest

from ocx import cli, config, discovery, updates


@pytest.mark.parametrize("version, notice", [("0.4.10", True), ("0.4.2", False), (None, False)])
def test_package_update_check(monkeypatch, capsys, version, notice):
    def fetch(url, timeout):
        assert url == updates.UPSTREAM_VERSION and timeout == 2
        if version is None:
            raise TimeoutError
        return io.BytesIO(f'[project]\nversion = "{version}"\n'.encode())

    monkeypatch.setattr(updates, "urlopen", fetch)
    monkeypatch.setattr(cli, "__version__", "0.4.3")
    cli._check_update()
    assert ("is available" in capsys.readouterr().out) == notice


def test_restart_runs_start_with_the_selected_config(tmp_path, monkeypatch):
    path = tmp_path / "ocx.yaml"
    path.write_text("listen: 127.0.0.1:19001\n")
    monkeypatch.setattr(cli, "_check_update", lambda: None)

    def start(cfg, args):
        assert cfg.path == path and cfg.listen_port == 19001
        return 7

    monkeypatch.setattr(cli, "cmd_start", start)
    assert cli.main(["--config", str(path), "restart"]) == 7


def test_status_checks_updates_and_explains_gpt_catalog_gaps(tmp_path, monkeypatch, capsys):
    yaml = tmp_path / "opencodex.yaml"
    yaml.write_text("openai:\n  include: [gpt-old, gpt-new, gpt-beta]\n  exclude_regex: '-beta$'\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [{"slug": "gpt-old"}]}))
    (tmp_path / "config.toml").write_text(f'model_catalog_json = "{catalog}"\n')
    monkeypatch.setattr(cli, "_health", lambda cfg: None)
    monkeypatch.setattr(discovery, "load_routes", lambda: [])
    monkeypatch.setattr(discovery, "read_openai_auth", lambda _: discovery.OpenAIAuth("chatgpt"))

    def models(cfg: config.Config, auth):
        assert cfg.openai_include == [] and cfg.openai_exclude_regex is None
        return [{"slug": s} for s in ("gpt-old", "gpt-new", "gpt-beta", "gpt-excluded")]

    monkeypatch.setattr(discovery, "_openai_models", models)
    monkeypatch.setattr(updates, "urlopen", lambda *args, **kwargs: io.BytesIO(b'[project]\nversion = "9.0.0"\n'))
    assert cli.main(["--config", str(yaml), "status"]) == 0
    out = capsys.readouterr().out
    assert "9.0.0 is available" in out
    assert "GPT in configured catalog: gpt-old" in out
    assert "GPT excluded by openai.include: gpt-excluded" in out
    assert "GPT excluded by openai.exclude_regex: gpt-beta" in out
    assert out.split("GPT available but missing from catalog: ")[1].splitlines()[0] == "gpt-new (run `ocx start`)"


def test_restart_leaves_an_unmanaged_codex_server_to_its_owner(tmp_path, monkeypatch, capsys):
    import socket

    cfg = config.Config(path=None, codex_home=tmp_path, listen_host="127.0.0.1", listen_port=0,
                        provider_id="ocx", openai_enabled=True, openai_include=[], providers=[])
    control = tmp_path / "app-server-control"
    control.mkdir()
    catalog = tmp_path / "catalog.json"

    def run(cmd, **kwargs):
        assert cmd == ["codex", "app-server", "daemon", "version"]
        return cli.subprocess.CompletedProcess(cmd, 0, '{"status":"running","managedCodexVersion":null}')

    monkeypatch.setattr(cli.subprocess, "run", run)
    with socket.socket(socket.AF_UNIX) as server:
        # Use a short relative path for the Unix socket on macOS.
        monkeypatch.chdir(tmp_path)
        server.bind("app-server-control/app-server-control.sock")
        catalog.touch()
        cli.os.utime(catalog, (2_000_000_000, 2_000_000_000))
        cli._restart_stale_codex(cfg, catalog)
    assert "Reconnect the Codex SSH session" in capsys.readouterr().out
