"""Repository-relative path safety helpers (shared by A's and C's code)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

DENIED_PARTS = frozenset({".git", ".depbot", ".env"})


class UnsafePath(ValueError):
    pass


def safe_join(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside ``root``; reject absolute paths, traversal,
    symlink escapes and denied components such as ``.git``."""
    if not rel_path or "\0" in rel_path:
        raise UnsafePath(f"invalid path {rel_path!r}")
    posix = PurePosixPath(rel_path.replace("\\", "/"))
    if posix.is_absolute() or (posix.parts and posix.parts[0].endswith(":")):
        raise UnsafePath(f"absolute path not allowed: {rel_path!r}")
    if ".." in posix.parts:
        raise UnsafePath(f"path traversal not allowed: {rel_path!r}")
    if DENIED_PARTS.intersection(posix.parts):
        raise UnsafePath(f"path touches a protected location: {rel_path!r}")
    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*posix.parts)).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise UnsafePath(f"path escapes the repository: {rel_path!r}")
    return candidate


def is_allowed(rel_path: str, allowed_paths: list[str]) -> bool:
    """True if ``rel_path`` equals or lies under one of ``allowed_paths``.
    An empty allow-list allows nothing."""
    posix = PurePosixPath(rel_path.replace("\\", "/"))
    for allowed in allowed_paths:
        base = PurePosixPath(allowed.rstrip("/"))
        if posix == base or base in posix.parents:
            return True
    return False
