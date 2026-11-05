"""Read-only Git queries with timeouts (shell=False). Never mutates a checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 30


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_git_tracked(root: Path) -> bool:
    proc = _git(root, "rev-parse", "--is-inside-work-tree")
    return proc is not None and proc.returncode == 0 and proc.stdout.strip() == "true"


def toplevel(root: Path) -> Path | None:
    proc = _git(root, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def current_branch(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if proc is None or proc.returncode != 0 or proc.stdout.strip() in ("", "HEAD"):
        return None
    return proc.stdout.strip()


def head_commit(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def dirty_paths(root: Path) -> list[str] | None:
    """Uncommitted/untracked paths under ``root`` (works for subdirectories of a
    larger repo). Returns None when ``root`` is not inside a Git work tree."""
    if not is_git_tracked(root):
        return None
    proc = _git(root, "status", "--porcelain", "--untracked-files=normal", "--", ".")
    if proc is None or proc.returncode != 0:
        return None
    return [line[3:] for line in proc.stdout.splitlines() if line.strip()]
