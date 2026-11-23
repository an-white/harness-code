"""Git repository detection helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .policy import detect_owner_repo
from .shell import (
    git_diff as _git_diff,
    git_log as _git_log,
    git_show as _git_show,
    git_status as _git_status,
)


def list_repos(workspace_root: Any) -> list[Path]:
    """Find all ``.git`` ancestors within depth 4 of ``workspace_root``."""
    root = Path(workspace_root).expanduser().resolve()
    repos: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        if depth > 4:
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            repos.add(Path(dirpath).resolve())
        if ".git" in dirnames:
            dirnames.remove(".git")
    return sorted(repos)


# Re-export helpers for convenience.
git_status = _git_status
git_diff = _git_diff
git_log = _git_log
git_show = _git_show


__all__ = [
    "detect_owner_repo",
    "list_repos",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
]