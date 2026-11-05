import subprocess
from pathlib import Path

from depbot.migration.patch_validator import validate_and_apply
from depbot.models import PatchStatus, SourcePatch


def _git(cwd: Path, *args: str):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, shell=False)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "src").mkdir()
    (path / "src" / "main.cpp").write_text("int value = old_sum(1, 2);\n", encoding="utf-8")
    _git(path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "-A")
    _git(path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    return path


def _patch(path="src/main.cpp", old="old_sum", new="sum"):
    return SourcePatch(
        patch_id="test-patch",
        unified_diff=(
            f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
            f"-int value = {old}(1, 2);\n+int value = {new}(1, 2);\n"
        ),
        files=[path],
    )


def test_valid_diff_is_checked_applied_and_whitespace_checked(tmp_path: Path):
    worktree = _repo(tmp_path / "repo")
    result = validate_and_apply(worktree=worktree, patch=_patch(), allowed_paths=["src"])
    assert result.status == PatchStatus.APPLIED
    assert "sum(" in (worktree / "src" / "main.cpp").read_text(encoding="utf-8")


def test_rejects_absolute_traversal_and_unauthorized_paths(tmp_path: Path):
    worktree = _repo(tmp_path / "repo")
    absolute = _patch("C:/outside.cpp")
    traversal = _patch("../outside.cpp")
    denied = _patch("README.md")
    assert validate_and_apply(worktree=worktree, patch=absolute, allowed_paths=["src"]).status == PatchStatus.REJECTED
    assert validate_and_apply(worktree=worktree, patch=traversal, allowed_paths=["src"]).status == PatchStatus.REJECTED
    assert validate_and_apply(worktree=worktree, patch=denied, allowed_paths=["src"]).status == PatchStatus.REJECTED


def test_rejects_stale_or_malformed_diff_without_editing(tmp_path: Path):
    worktree = _repo(tmp_path / "repo")
    stale = _patch(old="not_the_current_symbol")
    result = validate_and_apply(worktree=worktree, patch=stale, allowed_paths=["src"])
    assert result.status == PatchStatus.REJECTED
    assert "old_sum" in (worktree / "src" / "main.cpp").read_text(encoding="utf-8")


def test_declared_paths_must_match_diff_paths(tmp_path: Path):
    worktree = _repo(tmp_path / "repo")
    patch = _patch()
    patch = patch.model_copy(update={"files": ["README.md"]})
    result = validate_and_apply(worktree=worktree, patch=patch, allowed_paths=["src", "README.md"])
    assert result.status == PatchStatus.REJECTED
