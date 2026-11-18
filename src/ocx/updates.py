"""Read the version published on GitHub without installing anything."""

from urllib.request import urlopen

import tomlkit
from tomlkit.exceptions import ParseError

UPSTREAM_VERSION = "https://raw.githubusercontent.com/anhvth/opencodex-desktop/main/pyproject.toml"


def version_parts(version: str) -> tuple[int, ...]:
    return tuple(map(int, version.split(".")))


def latest_version() -> str | None:
    try:
        with urlopen(UPSTREAM_VERSION, timeout=2) as response:
            version = tomlkit.parse(response.read(4096).decode())["project"]["version"]
        if isinstance(version, str) and version.count(".") == 2 and all(
            part.isdecimal() for part in version.split(".")
        ):
            return version
    except (OSError, ParseError, KeyError, TypeError, ValueError):
        pass
    return None
