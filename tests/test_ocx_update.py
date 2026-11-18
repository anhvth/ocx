import subprocess
from types import SimpleNamespace

import pytest

from ocx import cli


@pytest.mark.parametrize(("latest", "install_code", "config_path", "start_code", "expected"), [
    ("0.4.10", 0, None, 0, 0),
    ("0.4.4", 0, "my config.yaml", 9, 9),
    ("0.4.10", 7, None, None, 7),
    ("0.4.3", None, None, None, 0),
    (None, None, None, None, 1),
])
def test_update_installs_and_starts_published_nondowngrades(
    monkeypatch, latest, install_code, config_path, start_code, expected,
):
    calls = []

    def run(cmd, *, check=False):
        calls.append(cmd)
        if check and install_code:
            raise subprocess.CalledProcessError(install_code, cmd)
        return SimpleNamespace(returncode=start_code)

    def tool_bin(cmd, *, text):
        calls.append(cmd)
        assert text
        return "/tmp/tool bin\n"

    monkeypatch.setattr(cli, "__version__", "0.4.4")
    monkeypatch.setattr(cli, "_latest_version", lambda: latest)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.subprocess, "check_output", tool_bin)
    config_args = ["--config", config_path] if config_path else []
    assert cli.main([*config_args, "update"]) == expected
    wanted = []
    if install_code is not None:
        wanted.append([
            "uv", "tool", "install", "--force", "--refresh",
            "git+https://github.com/anhvth/opencodex-desktop.git@main",
        ])
    if install_code == 0:
        wanted += [["uv", "tool", "dir", "--bin"], ["/tmp/tool bin/ocx", *config_args, "start"]]
    assert calls == wanted
