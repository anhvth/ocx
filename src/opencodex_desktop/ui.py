"""Small terminal output helpers: ✓ / ⚠ / ✗ lines, colored when stdout is a TTY."""

from __future__ import annotations

import os
import sys

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def head(text: str) -> None:
    print(_c("1", text))


def ok(text: str) -> None:
    print(f"  {_c('32', '✓')} {text}")


def warn(text: str) -> None:
    print(f"  {_c('33', '⚠')} {text}")


def err(text: str) -> None:
    print(f"  {_c('31', '✗')} {text}")


def info(text: str) -> None:
    print(f"    {_c('2', text)}")


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "?"
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)
