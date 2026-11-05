from pathlib import Path

from depbot.migration.patch_generator import generate_patches
from depbot.migration.planner import plan_migration
from depbot.models import (
    AnalysisMethod,
    BreakingChange,
    ChangeType,
    CodeUsage,
    Dependency,
    ImpactLevel,
    PackageManagerName,
)


def _change(**updates):
    data = {
        "id": "toydep-2-BC1",
        "title": "old_sum was removed",
        "change_type": ChangeType.REMOVED,
        "affected_symbols": ["old_sum"],
        "replacement_symbols": ["sum"],
        "migration_advice": "Use sum.",
        "evidence_urls": ["file:///release-notes.md"],
        "evidence_excerpt": "old_sum was removed and replaced by sum",
        "confidence": 0.98,
    }
    data.update(updates)
    return BreakingChange(**data)


def _usage(*, impact=ImpactLevel.DEFINITE, line=2, snippet="return old_sum(2, 3);"):
    return CodeUsage(
        symbol="old_sum",
        file_path="src/main.cpp",
        line_start=line,
        line_end=line,
        snippet=snippet,
        analysis_method=AnalysisMethod.RIPGREP,
        impact=impact,
        related_change_ids=["toydep-2-BC1"],
        confidence=0.95,
    )


def _dependency():
    return Dependency(
        name="toydep", current_version="1.0.0", target_version="2.0.0",
        package_manager=PackageManagerName.CONAN, manifest_path="conanfile.txt",
    )


def test_planner_maps_only_definite_evidence_backed_usages():
    plan = plan_migration(
        dependency=_dependency(),
        changes=[_change()],
        usages=[_usage(), _usage(impact=ImpactLevel.POSSIBLE, line=3)],
    )
    assert len(plan) == 1
    assert plan[0].file_path == "src/main.cpp"
    assert plan[0].related_change_ids == ["toydep-2-BC1"]
    assert plan[0].related_usage_lines == [2]


def test_planner_skips_ambiguous_replacements_and_unknown_impact():
    ambiguous = _change(replacement_symbols=["sum", "add"])
    assert not plan_migration(dependency=_dependency(), changes=[ambiguous], usages=[_usage()])
    assert not plan_migration(
        dependency=_dependency(), changes=[_change()], usages=[_usage(impact=ImpactLevel.UNKNOWN)]
    )


def test_patch_generator_updates_multiple_usages_and_preserves_other_code(tmp_path: Path):
    source = tmp_path / "src" / "main.cpp"
    source.parent.mkdir()
    source.write_text("int a = old_sum(1, 2);\nint b = old_sum(a, 3);\nint c = 9;\n", encoding="utf-8")
    usages = [
        _usage(line=1, snippet="int a = old_sum(1, 2);"),
        _usage(line=2, snippet="int b = old_sum(a, 3);"),
    ]
    plan = plan_migration(dependency=_dependency(), changes=[_change()], usages=usages)
    patches = generate_patches(worktree=tmp_path, plan=plan, changes=[_change()], usages=usages)
    assert len(patches) == 1
    added_lines = [
        line for line in patches[0].unified_diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert sum(line.count("sum(") for line in added_lines) == 2
    changed_lines = [
        line for line in patches[0].unified_diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert not any("int c = 9;" in line for line in changed_lines)


def test_patch_generator_rejects_stale_evidence(tmp_path: Path):
    path = tmp_path / "src" / "main.cpp"
    path.parent.mkdir()
    path.write_text("int main() { return old_sum(2, 3); }\n", encoding="utf-8")
    usage = _usage(line=1, snippet="return old_sum(4, 5);")
    plan = plan_migration(dependency=_dependency(), changes=[_change()], usages=[usage])
    try:
        generate_patches(worktree=tmp_path, plan=plan, changes=[_change()], usages=[usage])
    except Exception as exc:
        assert "snippet changed" in str(exc)
    else:
        raise AssertionError("stale source evidence should stop patch generation")
