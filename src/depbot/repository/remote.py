"""Remote (GitHub) repositories: a DepBot-owned local cache per repository.

    <state_dir>/sources/<repository_id>-<url hash>/   ← clone of the remote, default branch

Every run fetches the cache and resets it to the remote branch. Jobs then
create their isolated working copy *from the cache* exactly as for a local
repository (``repository.worktree``), so all existing safety checks apply.
The cache keeps the real ``origin``, which is what PR delivery pushes to.

Safety:
* Only ``https://``, ``ssh://`` and scp-style ``git@host:owner/repo`` URLs.
* URLs embedding credentials (``https://user:token@…``) are refused: use the
  Git/gh credential helper instead, so secrets never land in config or logs.
* Arguments are passed after ``--`` and URLs starting with ``-`` are refused
  (no option injection); argv only, timeouts, ``GIT_TERMINAL_PROMPT=0``.
* Nobody edits the cache. If it has local changes anyway, sync refuses.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from depbot.models import RepositoryRecord
from depbot.repository.git_info import dirty_paths, head_commit

CLONE_TIMEOUT_SECONDS = 600
FETCH_TIMEOUT_SECONDS = 300
ALLOWED_SCHEMES: frozenset[str] = frozenset({"https", "ssh"})  # tests may widen this
_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


class RemoteRepositoryError(RuntimeError):
    """Explained refusal or failure while syncing a remote repository."""


class SyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    path: str  # local cache checkout used as the job's source repository
    branch: str
    commit: str
    cloned: bool  # True on first sync, False when fetched


def is_remote(ref: str) -> bool:
    return "://" in ref or bool(_SCP_LIKE.match(ref.strip()))


def validate_url(url: str) -> str:
    url = url.strip()
    if not url or url.startswith("-") or any(c.isspace() or ord(c) < 32 for c in url):
        raise RemoteRepositoryError(f"refusing malformed repository URL {url!r}")
    if _SCP_LIKE.match(url):
        return url
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_SCHEMES))
        raise RemoteRepositoryError(f"unsupported repository URL scheme {parts.scheme!r} (allowed: {allowed})")
    if parts.password or (parts.scheme == "https" and parts.username):
        raise RemoteRepositoryError(
            "repository URLs must not embed credentials; configure git/gh credentials instead"
        )
    if parts.scheme != "file" and not parts.hostname:
        raise RemoteRepositoryError(f"repository URL has no host: {url!r}")
    return url


def cache_dir_for(repo: RepositoryRecord, sources_dir: Path) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "-", repo.repository_id).strip(".-") or "repo"
    digest = hashlib.sha256(repo.repo_url_or_path.strip().encode()).hexdigest()[:8]
    return sources_dir / f"{safe_id}-{digest}"


def _git(cwd: Path, *args: str, timeout: int = FETCH_TIMEOUT_SECONDS) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # never block on an interactive prompt
    try:
        proc = subprocess.run(
            ["git", "-c", "advice.detachedHead=false", *args],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteRepositoryError(f"git {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise RemoteRepositoryError(f"git could not run: {exc}") from exc
    if proc.returncode != 0:
        raise RemoteRepositoryError(f"git {args[0]} failed: {proc.stderr.strip()[-400:]}")
    return proc.stdout.strip()


def _remote_default_branch(cache: Path) -> str:
    ref = _git(cache, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    return ref.removeprefix("refs/remotes/origin/")


def sync_remote(repo: RepositoryRecord, sources_dir: Path) -> SyncResult:
    """Clone (first time) or fetch the remote, then check out the tip of its branch."""
    url = validate_url(repo.repo_url_or_path)
    branch = repo.default_branch
    if branch is not None and (not _BRANCH.match(branch) or branch.startswith("-")):
        raise RemoteRepositoryError(f"refusing suspicious branch name {branch!r}")
    cache = cache_dir_for(repo, sources_dir.resolve())
    cloned = not (cache / ".git").is_dir()
    if cloned:
        if cache.exists():
            raise RemoteRepositoryError(f"cache path exists but is not a Git clone: {cache}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        _git(cache.parent, "clone", "--quiet", "--no-single-branch", "--", url, str(cache),
             timeout=CLONE_TIMEOUT_SECONDS)
    else:
        if _git(cache, "remote", "get-url", "origin") != url:
            raise RemoteRepositoryError(f"cache {cache} points at a different origin; remove it to re-clone")
        dirty = dirty_paths(cache)
        if dirty:
            raise RemoteRepositoryError(
                f"DepBot's cache {cache} has local changes ({', '.join(dirty[:3])}); refusing to discard them"
            )
        _git(cache, "fetch", "--quiet", "--prune", "origin")
    branch = branch or _remote_default_branch(cache)
    # The cache only ever mirrors the remote: move the local branch to the remote tip.
    _git(cache, "checkout", "--quiet", "-B", branch, f"refs/remotes/origin/{branch}")
    commit = head_commit(cache)
    if commit is None:
        raise RemoteRepositoryError(f"remote branch {branch!r} has no commits")
    return SyncResult(url=url, path=str(cache), branch=branch, commit=commit, cloned=cloned)
