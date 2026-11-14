"""Small terminal output helpers: ✓ / ⚠ / ✗ lines, colored when stdout is a TTY."""

from __future__ import annotations

import os
import sys

# Colors on a terminal; FORCE_COLOR=1 forces them (ssh without -t, logs), NO_COLOR=1 disables them.
_COLOR = os.environ.get("NO_COLOR") is None and (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR")))
GREEN, YELLOW, RED = "32", "33", "31"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def head(text: str) -> None:
    print(_c("1", text))


def ok(text: str) -> None:
    print(f"  {_c(GREEN, '✓ ' + text)}")


def warn(text: str) -> None:
    print(f"  {_c('1;' + YELLOW, '⚠')} {_c(YELLOW, text)}")


def err(text: str) -> None:
    print(f"  {_c('1;' + RED, '✗')} {_c(RED, text)}")


def paint(line: str) -> str:
    """Color a log line by its leading status symbol (✓ green, ⚠ yellow, ✗ red)."""
    for sym, code in (("✓", GREEN), ("⚠", YELLOW), ("✗", RED)):
        if line.lstrip().startswith(sym) or f" {sym} " in line:
            return f"\033[{code}m{line}\033[0m"
    return line


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
