import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from ocx import cli, router


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
    monkeypatch.setattr(cli, "latest_version", lambda: latest)
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


def test_router_checks_daily_and_only_notifies_on_new_updates(monkeypatch):
    monkeypatch.setattr(router, "__version__", "1.0.0")
    monkeypatch.setattr(router, "load_routes", lambda: [])
    monkeypatch.setattr(router, "router_token", lambda: "test")
    instance = router.Router(SimpleNamespace(providers=[]))
    versions = iter(["1.1.0", None, "1.1.0", "1.2.0", "1.0.0"])
    monkeypatch.setattr(router, "latest_version", lambda: next(versions))
    notices, checks = [], []
    monkeypatch.setattr(router, "_log", notices.append)

    async def sleep(seconds):
        checks.append((seconds, instance.update_available))
        if len(checks) == 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(router.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance.watch_updates())
    assert checks == [(5, None), *[(86400, v) for v in ("1.1.0", "1.1.0", "1.1.0", "1.2.0", None)]]
    assert notices == [f"⚠ ocx {v} is available; run `ocx update`" for v in ("1.1.0", "1.2.0")]
