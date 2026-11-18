from types import SimpleNamespace

import pytest

from ocx import cli


@pytest.mark.parametrize(("latest", "install_code", "expected"), [
    ("0.4.10", 0, 0),
    ("0.4.4", 0, 0),
    ("0.4.10", 7, 7),
    ("0.4.3", None, 0),
    (None, None, 1),
])
def test_update_installs_only_published_nondowngrades(monkeypatch, capsys, latest, install_code, expected):
    calls = []

    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=install_code)

    monkeypatch.setattr(cli, "__version__", "0.4.4")
    monkeypatch.setattr(cli, "_latest_version", lambda: latest)
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["update"]) == expected
    if install_code is None:
        assert not calls
    else:
        assert calls == [[
            "uv", "tool", "install", "--force", "--refresh",
            "git+https://github.com/anhvth/opencodex-desktop.git@main",
        ]]
    assert ("run `ocx start`" in capsys.readouterr().out) == (install_code == 0)
