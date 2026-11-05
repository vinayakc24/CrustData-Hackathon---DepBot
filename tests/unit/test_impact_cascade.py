"""Contract + engine support for the impact cascade (I2 AST, I3 compiler probe)."""

import json

import pytest
from pydantic import ValidationError

from depbot.interfaces import StageError
from depbot.models import (
    AnalysisMethod,
    CodeUsage,
    CompilerDiagnostic,
    OutcomeStatus,
    ResolutionLayer,
    VerificationResult,
)
from depbot.orchestrator import run_repository_job
from depbot.repository.compile_db import external_include_dirs, include_dirs, load_compile_commands
from depbot.state import JobStatus
from tests.unit.test_orchestrator import USAGE, _ok, _passing, _stages, job  # noqa: F401


# ------------------------------------------------------------------ contracts


def test_llm_can_never_make_a_usage_definite():
    with pytest.raises(ValidationError, match="never make a usage 'definite'"):
        CodeUsage.model_validate(USAGE.model_dump() | {"resolved_by": "llm"})  # impact=definite
    possible = CodeUsage.model_validate(USAGE.model_dump() | {"resolved_by": "llm", "impact": "possible"})
    assert possible.resolved_by == ResolutionLayer.LLM


def test_cascade_provenance_fields_round_trip():
    usage = CodeUsage.model_validate(USAGE.model_dump() | {
        "analysis_method": "clang_ast", "resolved_by": "clang_ast",
        "resolved_usr": "c:@N@toydep@F@old_sum#I#I#", "declaration_file": "/conan/p/include/toydep/toydep.hpp",
        "resolution_proof": "references c:@N@toydep@F@old_sum#I#I#"})
    assert usage.analysis_method == AnalysisMethod.CLANG_AST
    probe = CodeUsage.model_validate(USAGE.model_dump() | {
        "analysis_method": "compiler_diagnostic", "resolved_by": "compiler_probe"})
    assert probe.resolved_by == ResolutionLayer.COMPILER_PROBE
    ruled_out = CodeUsage.model_validate(USAGE.model_dump() | {
        "impact": "unaffected", "resolved_by": "clang_ast",
        "resolution_proof": "references a different entity c:@N@mylib@F@old_sum#I#I#"})
    assert ruled_out.impact == "unaffected"


def test_compiler_diagnostic_model():
    d = CompilerDiagnostic(file_path="src/main.cpp", line=2, column=20, severity="error",
                           message="'old_sum' was not declared in this scope", command_label="probe.build")
    assert d.line == 2
    with pytest.raises(ValidationError):
        CompilerDiagnostic(file_path=None, line=0, severity="error", message="x", command_label="b")


# ------------------------------------------------------------------ probe step


def _probe_aware_verify(record, probe_result=None, raise_in_probe=None):
    def verify(*, worktree, adapter, phase, policy, artifacts_dir):
        record.append((phase.value, worktree, (worktree / "conanfile.txt").read_text(), artifacts_dir))
        if phase == "probe":
            if raise_in_probe:
                raise raise_in_probe
            return probe_result or _passing(phase)
        return _passing(phase)
    return verify


ERR = CompilerDiagnostic(file_path="src/main.cpp", line=2, column=21, severity="error",
                         message="'old_sum' was not declared in this scope", command_label="probe.build")


def _failing_probe():
    return VerificationResult(phase="probe", dependency_install=_ok("install"), configure=_ok("configure"),
                              build=_ok("build", code=2), diagnostics=[ERR])


def test_probe_builds_target_version_in_scratch_copy_before_impact(job, tmp_path):
    calls, impact_saw = [], {}

    def impact(*, worktree, changes, artifacts_dir):
        impact_saw["diagnostics"] = json.loads((artifacts_dir / "probe" / "diagnostics.json").read_text())
        impact_saw["manifest"] = (worktree / "conanfile.txt").read_text()
        return [USAGE]

    stages = _stages(verify=_probe_aware_verify(calls, _failing_probe()), impact=impact)
    state, outcome = run_repository_job(job, stages, artifacts_root=tmp_path / "a")

    assert [c[0] for c in calls] == ["baseline", "probe", "upgraded"]
    _, probe_wt, probe_manifest, probe_artifacts = calls[1]
    assert "toydep/2.0.0" in probe_manifest and probe_wt != tmp_path / "a" / state.run_id / "worktree"
    assert probe_artifacts == tmp_path / "a" / state.run_id / "probe"
    assert not (probe_wt / ".git").exists()
    # The real worktree is still on the old version when impact runs; impact sees the diagnostics.
    assert "toydep/1.0.0" in impact_saw["manifest"]
    assert impact_saw["diagnostics"][0]["message"].startswith("'old_sum' was not declared")
    # Advisory: a failing probe build does not fail the job.
    assert state.status == JobStatus.VERIFIED and outcome.status == OutcomeStatus.VERIFIED
    assert state.repo_facts["probe"]["ran"] and state.repo_facts["probe"]["errors"] == 1
    assert state.repo_facts["probe"]["build_succeeded"] is False
    assert any(c.command_label == "build" and c.exit_code == 2 for c in state.command_results)
    assert "probe" not in state.changed_files and all(not f.startswith("probe") for f in state.changed_files)


def test_probe_disabled_by_policy(job, tmp_path):
    calls = []
    job2 = job.model_copy(update={"policy": job.policy.model_copy(update={"run_probe_build": False})})
    state, _ = run_repository_job(job2, _stages(verify=_probe_aware_verify(calls)), artifacts_root=tmp_path / "a")
    assert [c[0] for c in calls] == ["baseline", "upgraded"]
    assert state.repo_facts["probe"]["skipped"].startswith("disabled by policy") and state.probe is None


def test_probe_failure_to_run_is_recorded_and_job_continues(job, tmp_path):
    calls = []
    verify = _probe_aware_verify(calls, raise_in_probe=StageError("toolchain exploded"))
    state, _ = run_repository_job(job, _stages(verify=verify), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.VERIFIED
    assert "probe could not run: toolchain exploded" in state.repo_facts["probe"]["skipped"]


# ------------------------------------------------------------------ compile database


def test_baseline_compile_db_recorded_with_dependency_include_dirs(job, tmp_path):
    dep_inc = tmp_path / "conan" / "p" / "toydep" / "include"
    dep_inc.mkdir(parents=True)

    def verify(*, worktree, adapter, phase, policy, artifacts_dir):
        if phase == "baseline":
            build = worktree / "build" / "Release"
            build.mkdir(parents=True)
            (build / "compile_commands.json").write_text(json.dumps([{
                "directory": str(build), "file": str(worktree / "src" / "main.cpp"),
                "command": f"/usr/bin/c++ -isystem {dep_inc} -I{worktree}/src -O2 -o x.o -c "
                           f"{worktree}/src/main.cpp"}]))
        return _passing(phase)

    state, _ = run_repository_job(job, _stages(verify=verify), artifacts_root=tmp_path / "a")
    facts = state.repo_facts["compile_commands"]
    saved = tmp_path / "a" / state.run_id / "compile_commands.baseline.json"
    assert facts["baseline"] == str(saved) and saved.is_file()
    assert facts["external_include_dirs"] == [str(dep_inc.resolve())]
    assert "build" not in "".join(state.changed_files)


def test_compile_db_without_build_dir_is_none(job, tmp_path):
    state, _ = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    assert state.repo_facts["compile_commands"] is None


def test_compile_db_parsing(tmp_path):
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps([
        {"directory": str(tmp_path), "file": "src/a.cpp", "arguments": ["c++", "-Iinc", "-isystem", "/dep/inc", "-c", "src/a.cpp"]},
        {"directory": str(tmp_path), "file": str(tmp_path / "b.cpp"), "command": "c++ -I /dep2/inc -c b.cpp"},
    ]))
    entries = load_compile_commands(db)
    assert entries[0].file == str((tmp_path / "src" / "a.cpp").resolve())
    assert include_dirs(entries[0]) == [str((tmp_path / "inc").resolve()), "/dep/inc"]
    assert external_include_dirs(entries, tmp_path) == [str(__import__("pathlib").Path(p).resolve()) for p in ("/dep/inc", "/dep2/inc")]
    db.write_text("{}")
    with pytest.raises(ValueError):
        load_compile_commands(db)
