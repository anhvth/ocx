"""Recognize private dotfiles-managed settings links."""

import os
from pathlib import Path


def managed_local_link(path: Path) -> bool:
    if not path.is_symlink():
        return False
    repo = Path(os.environ.get("DOTFILES_DIR", "~/dotfiles")).expanduser().resolve()
    return path.resolve().is_relative_to(repo / "_local")
