"""Scope and context validation before applying a source patch."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from depbot.models import PatchStatus, SourcePatch
from depbot.repository.paths import UnsafePath, is_allowed, safe_join


_DIFF_PATH = re.compile(r"^(---|\+\+\+) [ab]/(.+?)(?:\t.*)?$", re.MULTILINE)


def _reject(patch: SourcePatch, reason: str) -> SourcePatch:
    return patch.model_copy(update={"status": PatchStatus.REJECTED, "rejection_reason": reason})


def _paths_in_diff(diff: str) -> list[str]:
    paths = [match.group(2).replace("\\", "/") for match in _DIFF_PATH.finditer(diff)]
    return sorted(set(paths))


def validate_and_apply(*, worktree: Path, patch: SourcePatch, allowed_paths: list[str]) -> SourcePatch:
    """Validate exact patch paths, then use git apply --check/apply and diff --check."""
    if not patch.unified_diff.strip() or not patch.files:
        return _reject(patch, "patch has no diff content or declared files")
    diff_paths = _paths_in_diff(patch.unified_diff)
    declared = sorted(set(path.replace("\\", "/") for path in patch.files))
    if not diff_paths or diff_paths != declared:
        return _reject(patch, f"diff paths {diff_paths} do not match declared paths {declared}")
    for rel_path in diff_paths:
        try:
            safe_join(worktree, rel_path)
        except UnsafePath as exc:
            return _reject(patch, str(exc))
        if not is_allowed(rel_path, allowed_paths):
            return _reject(patch, f"path is outside approved scope: {rel_path}")

    patch_file: Path | None = None
    applied = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".patch", delete=False, dir=worktree.parent
        ) as handle:
            handle.write(patch.unified_diff.encode("utf-8"))
            patch_file = Path(handle.name)
        check = subprocess.run(
            ["git", "apply", "--check", str(patch_file)],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip()[:1000]
            return _reject(patch, f"git apply --check failed: {detail}")
        apply = subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
        if apply.returncode != 0:
            detail = (apply.stderr or apply.stdout).strip()[:1000]
            return _reject(patch, f"git apply failed: {detail}")
        applied = True
        whitespace = subprocess.run(
            ["git", "diff", "--check"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
        if whitespace.returncode != 0:
            subprocess.run(
                ["git", "apply", "--reverse", str(patch_file)],
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
            detail = (whitespace.stderr or whitespace.stdout).strip()[:1000]
            return _reject(patch, f"git diff --check failed: {detail}")
        return patch.model_copy(update={"status": PatchStatus.APPLIED, "rejection_reason": None})
    except (OSError, subprocess.SubprocessError) as exc:
        if applied and patch_file is not None:
            subprocess.run(
                ["git", "apply", "--reverse", str(patch_file)],
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        return _reject(patch, f"patch validation command failed: {type(exc).__name__}: {exc}")
    finally:
        if patch_file is not None:
            patch_file.unlink(missing_ok=True)
