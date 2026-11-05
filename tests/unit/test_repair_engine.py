"""Repair proposals from upgraded-build diagnostics (accept and decline rules)."""

import subprocess

import pytest

from depbot.models import (BreakingChange, CommandResult, CompilerDiagnostic, Dependency, PlannedChange,
                           SourcePatch, VerificationResult)
from depbot.state import DepBotState
from depbot.verification.repair_engine import propose_repair

CHANGE = BreakingChange(id="toydep-2.0.0-BC1", title="old_sum removed", change_type="removed",
                        affected_symbols=["old_sum"], replacement_symbols=["sum"], migration_advice="use sum",
                        evidence_urls=["file:///notes.md"], confidence=0.95)


@pytest.fixture
def wt(tmp_path):
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.cpp").write_text('#include "calls.inc"\nint main() { return toydep::sum(2, 3) == 5 ? 0 : 1; }\n')
    (root / "src" / "calls.inc").write_text("// helper\ninline int via_inc() { return toydep::old_sum(1, 1); }\n")
    (root / "src" / "paste.cpp").write_text("#define OLD(n) toydep::old_##n\nint f() { return OLD(sum)(1, 1); }\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _state(**kw):
    dep = Dependency(name="toydep", current_version="1.0.0", target_version="2.0.0",
                     package_manager="conan", manifest_path="conanfile.txt")
    base = dict(run_id="r", repository_id="repo_d", original_repo="/src", worktree_path="/w", base_branch="b",
                working_branch="w", dependency=dep, artifacts_dir="/a", breaking_changes=[CHANGE],
                migration_plan=[PlannedChange(file_path="src/main.cpp", reason="r", related_change_ids=[CHANGE.id],
                                              expected_change="x")])
    return DepBotState(**{**base, **kw})


def _failure(*diags, message="'old_sum' is not a member of 'toydep'"):
    build = CommandResult(command_label="upgraded.build", args=["cmake"], cwd="/w", exit_code=2,
                          duration_seconds=1, stderr_tail=f"error: {message}")
    ok = CommandResult(command_label="upgraded.configure", args=["cmake"], cwd="/w", exit_code=0, duration_seconds=1)
    return VerificationResult(phase="upgraded", dependency_install=ok, configure=ok, build=build,
                              diagnostics=list(diags))


def _diag(path, line, message="'old_sum' is not a member of 'toydep'"):
    return CompilerDiagnostic(file_path=path, line=line, severity="error", message=message,
                              command_label="upgraded.build")


def test_repairs_a_compiler_reported_usage_the_impact_analysis_missed(wt):
    (patch,) = propose_repair(worktree=wt, state=_state(), failure=_failure(_diag("src/calls.inc", 2)), attempt=1)
    assert patch.files == ["src/calls.inc"] and patch.patch_id.startswith("repair1-")
    assert "-inline int via_inc() { return toydep::old_sum(1, 1); }" in patch.unified_diff
    assert "+inline int via_inc() { return toydep::sum(1, 1); }" in patch.unified_diff
    assert patch.related_change_ids == [CHANGE.id]


@pytest.mark.parametrize("diag,why", [
    (_diag("src/paste.cpp", 2), "symbol produced by token pasting is not on the line"),
    (_diag("src/calls.inc", 1), "reported line does not contain the symbol"),
    (_diag("src/calls.inc", 2, "'scale' is not a member of 'toydep'"), "no evidence for this symbol"),
    (_diag("../outside.cpp", 1), "path outside the worktree"),
    (_diag(None, 2), "error outside the repository"),
])
def test_declines_without_evidence_or_exact_location(wt, diag, why):
    assert propose_repair(worktree=wt, state=_state(), failure=_failure(diag), attempt=1) == [], why


def test_declines_non_symbol_failures_out_of_range_attempts_and_known_diffs(wt):
    link = _failure(_diag("src/calls.inc", 2), message="undefined reference to `old_sum(int, int)'")
    link = link.model_copy(update={"diagnostics": []})
    assert propose_repair(worktree=wt, state=_state(), failure=link, attempt=1) == []
    good = _failure(_diag("src/calls.inc", 2))
    assert propose_repair(worktree=wt, state=_state(max_repair_attempts=2), failure=good, attempt=3) == []
    (first,) = propose_repair(worktree=wt, state=_state(), failure=good, attempt=1)
    applied = SourcePatch(patch_id="p", unified_diff=first.unified_diff, files=first.files, status="applied")
    assert propose_repair(worktree=wt, state=_state(patches=[applied]), failure=good, attempt=2) == []
    without_replacement = CHANGE.model_copy(update={"replacement_symbols": []})
    assert propose_repair(worktree=wt, state=_state(breaking_changes=[without_replacement]), failure=good, attempt=1) == []
