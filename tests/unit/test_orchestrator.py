"""Repository engine tests with fake B/C stages and a real isolated working copy.

The stages are fakes (they are other members' modules); the engine itself,
working-copy isolation, manifest update and scope checks run for real.
"""

import json
import subprocess

import pytest

from depbot.interfaces import PipelineStages, StageError
from depbot.models import (
    BreakingChange,
    CodeUsage,
    CommandResult,
    OutcomeStatus,
    PatchStatus,
    PlannedChange,
    ReleaseEvent,
    RepositoryDependencyRecord,
    RepositoryRecord,
    RepositoryUpgradeJob,
    SourcePatch,
    UpgradePolicy,
    VerificationResult,
)
from depbot.orchestrator import load_default_stages, run_repository_job
from depbot.state import JobStatus

MAIN = "#include <toydep/toydep.h>\nint main() { return old_sum(2, 3) == 5 ? 0 : 1; }\n"
DIFF = """\
--- a/src/main.cpp
+++ b/src/main.cpp
@@ -1,2 +1,2 @@
 #include <toydep/toydep.h>
-int main() { return old_sum(2, 3) == 5 ? 0 : 1; }
+int main() { return sum(2, 3) == 5 ? 0 : 1; }
"""

CHANGE = BreakingChange(
    id="toydep-2.0.0-BC1", title="old_sum removed", change_type="renamed",
    affected_symbols=["old_sum"], replacement_symbols=["sum"], migration_advice="call sum",
    evidence_urls=["file://CHANGELOG.md"], evidence_excerpt="old_sum was removed", confidence=0.95,
)
USAGE = CodeUsage(
    symbol="old_sum", file_path="src/main.cpp", line_start=2, line_end=2,
    snippet="old_sum(2, 3)", analysis_method="ripgrep", impact="definite",
    related_change_ids=[CHANGE.id], confidence=0.9,
)
PLAN = PlannedChange(
    file_path="src/main.cpp", reason="old_sum removed", related_change_ids=[CHANGE.id],
    expected_change="old_sum -> sum",
)


def _ok(label, code=0):
    return CommandResult(command_label=label, args=[label], cwd="/w", exit_code=code, duration_seconds=0.01)


def _passing(phase, **_):
    return VerificationResult(
        phase=phase, dependency_install=_ok("install"), configure=_ok("configure"),
        build=_ok("build"), tests=_ok("test"), tests_passed=1, tests_failed=0, passed=True,
    )


def _git_apply(*, worktree, patch, allowed_paths):
    proc = subprocess.run(["git", "apply", "-"], cwd=worktree, input=patch.unified_diff,
                          text=True, capture_output=True, timeout=30)
    status = PatchStatus.APPLIED if proc.returncode == 0 else PatchStatus.REJECTED
    return patch.model_copy(update={"status": status, "rejection_reason": proc.stderr or None})


def _stages(**overrides):
    reports = []

    def report(*, state, artifacts_dir):
        path = artifacts_dir / "upgrade-report.md"
        path.write_text(f"status: {state.status}\n")
        reports.append(path)
        return path

    base = dict(
        research=lambda **_: [CHANGE],
        impact=lambda **_: [USAGE],
        plan=lambda **_: [PLAN],
        patch=lambda **_: [SourcePatch(patch_id="p1", unified_diff=DIFF, files=["src/main.cpp"],
                                       related_change_ids=[CHANGE.id])],
        apply=_git_apply,
        verify=_passing,
        report=report,
    )
    base.update(overrides)
    return PipelineStages(**base)


@pytest.fixture
def job(tmp_path):
    repo = tmp_path / "repo_a"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.cpp").write_text(MAIN)
    (repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\nproject(a CXX)\n")
    (repo / "conanfile.txt").write_text("[requires]\ntoydep/1.0.0\n")
    event = ReleaseEvent(event_id="toydep-v2", dependency_name="toydep", upstream_repo="local/toydep",
                         previous_version="1.0.0", target_version="2.0.0")
    return RepositoryUpgradeJob(
        job_id="j1", event=event,
        repository=RepositoryRecord(repository_id="repo_a", repo_url_or_path=str(repo), display_name="A"),
        dependency_record=RepositoryDependencyRecord(
            repository_id="repo_a", dependency_name="toydep", current_version="1.0.0",
            package_manager="conan", manifest_path="conanfile.txt"),
        policy=UpgradePolicy(policy_id="t", allowed_change_levels=["major"], major_approved=True),
        repo_root=str(repo),
    )


def test_happy_path_verifies_and_never_touches_source(job, tmp_path):
    state, outcome = run_repository_job(job, _stages(), artifacts_root=tmp_path / "artifacts")
    assert state.status == JobStatus.VERIFIED, state.status_reason
    assert outcome.status == OutcomeStatus.VERIFIED and outcome.verification_passed
    assert outcome.changed_files == ["conanfile.txt", "src/main.cpp"]
    wt = tmp_path / "artifacts" / state.run_id / "worktree"
    assert "sum(2, 3)" in (wt / "src" / "main.cpp").read_text()
    assert "toydep/2.0.0" in (wt / "conanfile.txt").read_text()
    src = tmp_path / "repo_a"
    assert (src / "src" / "main.cpp").read_text() == MAIN
    assert "toydep/1.0.0" in (src / "conanfile.txt").read_text()
    art = tmp_path / "artifacts" / state.run_id
    for name in ("inspection.json", "breaking_changes.json", "impact.json", "plan.json",
                 "patches/manifest.diff", "patches/final.diff", "state.json", "upgrade-report.md"):
        assert (art / name).is_file(), name
    saved = json.loads((art / "state.json").read_text())
    assert [h["to_status"] for h in saved["history"]] == [
        "INSPECTED", "RESEARCHED", "IMPACT_ANALYZED", "PLANNED", "PATCHED", "VERIFIED"]
    assert state.baseline.phase == "baseline" and state.probe.phase == "probe"
    assert len(state.command_results) == 12  # baseline + probe + upgraded, 4 steps each


def test_no_usages_bumps_manifest_and_reports_unaffected(job, tmp_path):
    stages = _stages(impact=lambda **_: [], plan=lambda **_: [])
    state, outcome = run_repository_job(job, stages, artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.NO_CHANGES_NEEDED
    assert outcome.status == OutcomeStatus.UNAFFECTED and outcome.changed_files == ["conanfile.txt"]


def test_build_failure_is_reported_not_hidden(job, tmp_path):
    def failing(phase, **kw):
        if phase == "baseline":
            return _passing(phase)
        return VerificationResult(phase=phase, dependency_install=_ok("install"),
                                  configure=_ok("configure"), build=_ok("build", code=2))
    state, outcome = run_repository_job(job, _stages(verify=failing), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_BUILD and "build failed (exit code 2)" in state.status_reason
    assert outcome.status == OutcomeStatus.FAILED and not outcome.verification_passed


def test_failing_baseline_blocks_before_any_edit(job, tmp_path):
    def broken(phase, **kw):
        return VerificationResult(phase=phase, dependency_install=_ok("install"),
                                  configure=_ok("configure"), build=_ok("build", code=1))
    state, outcome = run_repository_job(job, _stages(verify=broken), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.BLOCKED and "baseline" in state.status_reason
    assert state.manifest_change is None


def test_out_of_scope_patch_is_blocked(job, tmp_path):
    evil = SourcePatch(patch_id="p2", unified_diff=DIFF, files=["CMakeLists.txt"])
    state, outcome = run_repository_job(job, _stages(patch=lambda **_: [evil]), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.BLOCKED and "outside approved scope" in state.status_reason


def test_traversal_in_stage_output_is_blocked(job, tmp_path):
    bad = USAGE.model_copy(update={"file_path": "../../etc/passwd"})
    state, _ = run_repository_job(job, _stages(impact=lambda **_: [bad]), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.BLOCKED and "unsafe path" in state.status_reason


def test_low_confidence_plan_requires_approval(job, tmp_path):
    shaky = PLAN.model_copy(update={"confidence": 0.4})
    state, _ = run_repository_job(job, _stages(plan=lambda **_: [shaky]), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.BLOCKED and "below minimum" in state.status_reason


def test_stage_errors_and_crashes_fail_only_this_job(job, tmp_path):
    def no_evidence(**_):
        raise StageError("no release evidence found")
    state, _ = run_repository_job(job, _stages(research=no_evidence), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_RESEARCH

    def crash(**_):
        raise KeyError("boom")
    state, outcome = run_repository_job(job, _stages(plan=crash), artifacts_root=tmp_path / "b")
    assert state.status == JobStatus.FAILED_MIGRATION and "internal error" in state.status_reason
    assert outcome.status == OutcomeStatus.FAILED


def test_revalidation_skips_repo_already_upgraded_since_indexing(job, tmp_path):
    (tmp_path / "repo_a" / "conanfile.txt").write_text("[requires]\ntoydep/2.0.0\n")
    state, outcome = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    assert outcome.status == OutcomeStatus.SKIPPED and "revalidation" in outcome.reason


def test_unintegrated_stages_block_honestly(job, tmp_path):
    from depbot.orchestrator import _not_integrated
    stages = _stages(research=_not_integrated("research", "depbot.research.x.research_release"))
    state, outcome = run_repository_job(job, stages, artifacts_root=tmp_path / "a")
    assert outcome.status == OutcomeStatus.BLOCKED
    assert "stage 'research' not integrated yet" in state.status_reason


def test_default_stages_resolve_integrated_modules():
    stages = load_default_stages()
    assert stages.verify.__module__ == "depbot.verification.pipeline"
    assert stages.plan.__module__ == "depbot.migration.planner"
    assert stages.impact.__module__ == "depbot.code_intelligence.impact_analyzer"
    assert stages.repair is not None


# ----------------------------------------------------------------- repair loop

NOOP_DIFF = """\
--- a/src/main.cpp
+++ b/src/main.cpp
@@ -1,2 +1,3 @@
 #include <toydep/toydep.h>
+// repaired
 int main() { return sum(2, 3) == 5 ? 0 : 1; }
"""


def _fails_until(n_good_after):
    """Upgraded verification fails to build until `n_good_after` repairs were applied."""
    calls = {"upgraded": 0}

    def verify(phase, **kw):
        if phase in ("baseline", "probe"):
            return _passing(phase)
        calls["upgraded"] += 1
        if calls["upgraded"] > n_good_after:
            return _passing(phase)
        return VerificationResult(phase=phase, dependency_install=_ok("install"),
                                  configure=_ok("configure"), build=_ok("build", code=2),
                                  failure_category="removed_symbol")
    return verify, calls


def test_repair_fixes_build_then_verifies(job, tmp_path):
    verify, calls = _fails_until(1)
    seen = []

    def repair(*, worktree, state, failure, attempt):
        seen.append((attempt, failure.failure_category, state.status))
        return [SourcePatch(patch_id=f"r{attempt}", unified_diff=NOOP_DIFF, files=["src/main.cpp"])]

    state, outcome = run_repository_job(job, _stages(verify=verify, repair=repair), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.VERIFIED and outcome.status == OutcomeStatus.VERIFIED
    assert seen == [(1, "removed_symbol", JobStatus.FAILED_BUILD)]
    assert state.repair_attempts == 1 and calls["upgraded"] == 2
    assert "// repaired" in (tmp_path / "a" / state.run_id / "worktree" / "src" / "main.cpp").read_text()
    assert (tmp_path / "a" / state.run_id / "patches" / "r1.diff").is_file()


def test_repair_loop_stops_at_policy_limit(job, tmp_path):
    verify, calls = _fails_until(99)
    counter = {"n": 0}

    def repair(**kw):
        counter["n"] += 1
        body = NOOP_DIFF.replace("// repaired", f"// repaired {counter['n']}")
        if counter["n"] > 1:  # second repair edits on top of the first
            body = body.replace(" int main", f" // repaired {counter['n'] - 1}\n int main", 1)
            body = body.replace("@@ -1,2 +1,3 @@", "@@ -1,3 +1,4 @@")
        return [SourcePatch(patch_id=f"r{counter['n']}", unified_diff=body, files=["src/main.cpp"])]

    job2 = job.model_copy(update={"policy": job.policy.model_copy(update={"max_repair_attempts": 2})})
    state, outcome = run_repository_job(job2, _stages(verify=verify, repair=repair), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_BUILD and outcome.status == OutcomeStatus.FAILED
    assert state.repair_attempts == 2 and counter["n"] == 2 and calls["upgraded"] == 3
    assert "repair limit 2 reached" in state.status_reason


def test_identical_repair_patch_is_not_retried(job, tmp_path):
    verify, _ = _fails_until(99)
    repair = lambda **kw: [SourcePatch(patch_id="same", unified_diff=DIFF, files=["src/main.cpp"])]
    state, _ = run_repository_job(job, _stages(verify=verify, repair=repair), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_MIGRATION and "repeats an already-applied diff" in state.status_reason


def test_repair_giving_up_keeps_failure(job, tmp_path):
    verify, _ = _fails_until(99)
    state, _ = run_repository_job(job, _stages(verify=verify, repair=lambda **kw: []), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_BUILD and "no repair proposed" in state.status_reason
    assert state.repair_attempts == 0


@pytest.mark.real_tooling
def test_missing_conan_blocks_job_before_any_edit(job, tmp_path, monkeypatch):
    import depbot.package_managers.conan as conan_mod
    monkeypatch.setattr(conan_mod.shutil, "which", lambda name: None)
    state, outcome = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    assert outcome.status == OutcomeStatus.BLOCKED
    assert "conan tooling: conan executable not found on PATH" in state.status_reason
    assert state.manifest_change is None and state.baseline is None
