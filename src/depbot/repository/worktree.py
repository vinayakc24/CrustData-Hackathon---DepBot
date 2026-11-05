"""Isolated working copies. The source checkout is only ever read.

Each job gets ``artifacts/<run_id>/worktree``: a filtered copy of the source
repository, initialised as its own Git repository with a baseline commit on
branch ``depbot/<dependency>-<version>``. Stages patch/build only there, and
``git diff`` against the baseline is the complete record of DepBot's edits.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from depbot.repository.git_info import dirty_paths, head_commit

GIT_TIMEOUT_SECONDS = 60
# Directory/file names never copied into a working copy.
EXCLUDED_NAMES = (
    ".git",
    ".depbot",
    "build",
    "cmake-build-*",
    ".cache",
    "__pycache__",
    "CMakeUserPresets.json",
    ".env",
)
_GIT_IDENTITY = ["-c", "user.name=DepBot", "-c", "user.email=depbot@localhost", "-c", "commit.gpgsign=false"]


class UnsafeRepository(RuntimeError):
    """The source repository cannot be used safely (dirty, symlink escape, overlap)."""


class WorkingCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_root: str
    path: str
    branch: str
    base_branch: str = "depbot-baseline"
    source_commit: str | None
    baseline_commit: str
    version_controlled: bool
    notes: list[str] = Field(default_factory=list)


def branch_name(dependency: str, version: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in f"{dependency}-{version}")
    return f"depbot/{safe}"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if any(fnmatch.fnmatch(n, pat) for pat in EXCLUDED_NAMES)}


def _check_symlinks(source: Path) -> None:
    root = source.resolve()
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if not _ignore(dirpath, [d])]
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                target = path.resolve()
                if target != root and root not in target.parents:
                    raise UnsafeRepository(f"symlink escapes repository: {path} -> {target}")


def prepare_working_copy(
    source_root: Path,
    dest: Path,
    *,
    branch: str,
    refuse_dirty: bool = True,
) -> WorkingCopy:
    source = source_root.resolve()
    dest = dest.resolve()
    if not source.is_dir():
        raise UnsafeRepository(f"source repository not found: {source}")
    if dest == source or source in dest.parents:
        raise UnsafeRepository("working copy must not be inside the source repository")
    if dest.exists():
        raise UnsafeRepository(f"working copy destination already exists: {dest}")

    notes: list[str] = []
    dirty = dirty_paths(source)
    if dirty is None:
        notes.append("source is not under Git; dirty-tree check unavailable (copy is still isolated)")
    elif dirty:
        message = f"source has {len(dirty)} uncommitted change(s): {', '.join(dirty[:5])}"
        if refuse_dirty:
            raise UnsafeRepository(message + "; refusing (commit or stash them yourself)")
        notes.append(message + "; continuing on a copy as configured")

    _check_symlinks(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, symlinks=True, ignore=_ignore)

    _git(dest, "init", "-q", "-b", "depbot-baseline")
    # Build outputs created later must never show up as DepBot edits.
    (dest / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (dest / ".git" / "info" / "exclude").write_text(
        "\n".join(["build/", "cmake-build-*/", "CMakeUserPresets.json", ".cache/", ""]),
        encoding="utf-8",
    )
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", "DepBot baseline snapshot")
    baseline = _git(dest, "rev-parse", "HEAD")
    _git(dest, "checkout", "-q", "-b", branch)
    return WorkingCopy(
        source_root=str(source),
        path=str(dest),
        branch=branch,
        source_commit=head_commit(source),
        baseline_commit=baseline,
        version_controlled=dirty is not None,
        notes=notes,
    )


def changed_files(worktree: Path) -> list[str]:
    """Paths changed relative to the baseline commit (tracked + untracked, sans ignores)."""
    _git(worktree, "add", "-A", "--intent-to-add")
    out = _git(worktree, "diff", "--name-only", "depbot-baseline")
    return sorted(line for line in out.splitlines() if line)


def full_diff(worktree: Path) -> str:
    _git(worktree, "add", "-A", "--intent-to-add")
    return _git(worktree, "diff", "depbot-baseline")


def commit_all(worktree: Path, message: str) -> str:
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "--allow-empty", "-m", message)
    return _git(worktree, "rev-parse", "HEAD")
