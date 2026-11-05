from __future__ import annotations

from pathlib import Path

import pytest

from depbot.code_intelligence import impact_analyzer as analyzer
from depbot.code_intelligence.ripgrep_indexer import (
    RipgrepTimeout,
    UsageMatch,
)
from depbot.interfaces import StageError
from depbot.models import BreakingChange, ChangeType, ImpactLevel


def _change(change_id: str, symbols: list[str], replacements: list[str] | None = None) -> BreakingChange:
    return BreakingChange(
        id=change_id,
        title="API change",
        change_type=ChangeType.UNKNOWN,
        affected_symbols=symbols,
        replacement_symbols=replacements or [],
        migration_advice="unknown",
        evidence_urls=[],
        confidence=0.5,
    )


def _run(root: Path, changes: list[BreakingChange]):
    return analyzer.find_impacted_usages(worktree=root, changes=changes, artifacts_dir=root / "artifacts")


def test_empty_changes_and_no_usable_symbols_return_empty(tmp_path: Path) -> None:
    assert _run(tmp_path, []) == []
    assert _run(tmp_path, [_change("bc", ["", "  "], ["new_api"])]) == []


def test_matches_symbols_and_maps_shared_changes_conservatively(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text("Old::API();\nold_sum();\n", encoding="utf-8")
    changes = [
        _change("bc1", ["Old::API", "old_sum"], ["replacement"]),
        _change("bc2", ["old_sum"]),
    ]
    usages = _run(tmp_path, changes)
    assert [(u.symbol, u.file_path, u.line_start, u.line_end, u.snippet) for u in usages] == [
        ("Old::API", "src/main.cpp", 1, 1, "Old::API();"),
        ("old_sum", "src/main.cpp", 2, 2, "old_sum();"),
    ]
    assert usages[0].related_change_ids == ["bc1"]
    assert usages[1].related_change_ids == ["bc1", "bc2"]
    assert all(u.analysis_method.value == "ripgrep" for u in usages)
    assert all(u.impact == ImpactLevel.DEFINITE and u.confidence == 0.95 for u in usages)


def test_duplicate_matches_are_removed_and_output_order_is_stable(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "z.cpp").write_text("api();\n", encoding="utf-8")
    (tmp_path / "a.cpp").write_text("api();\n", encoding="utf-8")
    duplicate = UsageMatch("api", "z.cpp", 1, "api();")
    monkeypatch.setattr(analyzer, "search_symbols", lambda *a: [duplicate, duplicate, UsageMatch("api", "a.cpp", 1, "api();")])
    result = _run(tmp_path, [_change("bc", ["api"])])
    assert [(u.file_path, u.line_start) for u in result] == [("a.cpp", 1), ("z.cpp", 1)]


def test_invalid_worktree_becomes_stage_error(tmp_path: Path) -> None:
    with pytest.raises(StageError, match="worktree is unavailable"):
        _run(tmp_path / "missing", [_change("bc", ["api"])])


@pytest.mark.parametrize("failure", [RipgrepTimeout("expired"), FileNotFoundError("rg"), OSError("read failed")])
def test_indexer_failures_become_stage_errors(tmp_path: Path, monkeypatch, failure: Exception) -> None:
    monkeypatch.setattr(analyzer, "search_symbols", lambda *a: (_ for _ in ()).throw(failure))
    with pytest.raises(StageError, match="usage indexing"):
        _run(tmp_path, [_change("bc", ["api"])])


@pytest.mark.parametrize("match", [
    UsageMatch("unexpected", "src.cpp", 1, "api();"),
    UsageMatch("api", "../outside.cpp", 1, "api();"),
    UsageMatch("api", "src.cpp", 2, "api();"),
    UsageMatch("api", "src.cpp", 1, "wrong();"),
])
def test_inconsistent_matches_are_rejected(tmp_path: Path, monkeypatch, match: UsageMatch) -> None:
    (tmp_path / "src.cpp").write_text("api();\n", encoding="utf-8")
    monkeypatch.setattr(analyzer, "search_symbols", lambda *a: [match])
    with pytest.raises(StageError):
        _run(tmp_path, [_change("bc", ["api"])])


def test_worktree_is_not_modified(tmp_path: Path) -> None:
    source = tmp_path / "main.cpp"
    source.write_text("api();\n", encoding="utf-8")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    _run(tmp_path, [_change("bc", ["api"])])
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
