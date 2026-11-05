from __future__ import annotations

import shutil
from pathlib import Path

from depbot.code_intelligence.impact_analyzer import find_impacted_usages
from depbot.models import BreakingChange, ChangeType, ImpactLevel

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "toydep" / "repositories"
CHANGE_ID = "toydep-2.0.0-BC1"


def _breaking_change() -> BreakingChange:
    return BreakingChange(
        id=CHANGE_ID,
        title="old_sum was removed",
        change_type=ChangeType.REMOVED,
        affected_symbols=["old_sum"],
        replacement_symbols=["sum"],
        migration_advice="Replace old_sum with sum.",
        evidence_urls=["file:///toydep/release-notes-2.0.0.md"],
        evidence_excerpt="This release removes `toydep::old_sum(int, int)`.",
        confidence=0.95,
    )


def _copy_fixtures(destination: Path) -> dict[str, Path]:
    copies: dict[str, Path] = {}
    for name in ("repo_a", "repo_b", "repo_c"):
        target = destination / name
        shutil.copytree(FIXTURE_ROOT / name, target)
        copies[name] = target
    return copies


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _add_decoys(repo: Path) -> None:
    main_cpp = repo / "src" / "main.cpp"
    with main_cpp.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n// old_sum(1, 2) is mentioned in a comment.\n")
        stream.write('const char* example = "old_sum(1, 2)";\n')

    declaration = repo / "include" / "declaration.hpp"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        "int old_sum(int, int);\n#define old_sum(a, b) ((a) + (b))\n",
        encoding="utf-8",
    )

    for directory in (
        "build", "generated", ".git", ".cache", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", "cache", "vcpkg_installed", "vendor", "third_party", "__pycache__",
    ):
        path = repo / directory / "decoy.cpp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("int main() { return old_sum(1, 2); }\n", encoding="utf-8")


def test_fixture_repositories_keep_text_only_matches_possible(tmp_path: Path) -> None:
    copies = _copy_fixtures(tmp_path / "copies")
    original_before = {name: _snapshot(FIXTURE_ROOT / name) for name in copies}
    _add_decoys(copies["repo_a"])
    change = _breaking_change()

    repo_a_usages = find_impacted_usages(
        worktree=copies["repo_a"], changes=[change], artifacts_dir=tmp_path / "artifacts-a"
    )
    repo_b_usages = find_impacted_usages(
        worktree=copies["repo_b"], changes=[change], artifacts_dir=tmp_path / "artifacts-b"
    )
    repo_c_usages = find_impacted_usages(
        worktree=copies["repo_c"], changes=[change], artifacts_dir=tmp_path / "artifacts-c"
    )

    for usages in (repo_a_usages, repo_b_usages):
        actual_calls = [usage for usage in usages if usage.file_path == "src/main.cpp" and usage.line_start == 4]
        assert [(usage.symbol, usage.file_path, usage.line_start, usage.line_end) for usage in actual_calls] == [
            ("old_sum", "src/main.cpp", 4, 4)
        ]
        assert actual_calls[0].related_change_ids == [CHANGE_ID]
        assert actual_calls[0].impact == ImpactLevel.DEFINITE
        assert actual_calls[0].confidence == 0.95

    assert repo_c_usages == []

    # Clear call syntax can be confirmed by I1 when no compile database is present;
    # comments, declarations and literals remain uncertain.
    possible = [usage for usage in repo_a_usages if usage.impact == ImpactLevel.POSSIBLE]
    assert any("comment" in usage.snippet for usage in possible)
    assert any("example =" in usage.snippet for usage in possible)
    assert any(usage.file_path == "include/declaration.hpp" for usage in possible)
    assert not any(
        usage.file_path.startswith((
            "build/", "generated/", ".git/", ".cache/", ".pytest_cache/",
            ".mypy_cache/", ".ruff_cache/", "cache/", "vcpkg_installed/", "vendor/",
            "third_party/", "__pycache__/",
        ))
        for usage in repo_a_usages
    )
    assert {name: _snapshot(FIXTURE_ROOT / name) for name in copies} == original_before
