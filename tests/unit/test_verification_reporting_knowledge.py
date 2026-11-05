import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from depbot.knowledge.pattern_store import (
    KnowledgeStoreError,
    find_verified_patterns,
    store_verified_pattern,
)
from depbot.models import (
    CommandResult,
    AnalysisMethod,
    BreakingChange,
    ChangeType,
    CodeUsage,
    Dependency,
    ImpactLevel,
    MigrationPattern,
    MigrationVerification,
    PackageManagerName,
    UpgradePolicy,
    VerificationPhase,
    VerificationResult,
)
from depbot.reporting.evidence_collector import collect_evidence
from depbot.reporting.report_generator import write_repository_report
from depbot.migration.planner import plan_migration
from depbot.state import DepBotState, JobStatus
from depbot.package_managers.base import minimal_env
from depbot.verification.command_runner import MAX_LOG_BYTES, run_command
from depbot.verification.error_classifier import classify_failure
from depbot.verification.pipeline import _test_counts, run_verification
from depbot.verification.repair_engine import propose_repair


def _state(tmp_path: Path) -> DepBotState:
    return DepBotState(
        run_id="test-run",
        repository_id="repo-test",
        original_repo=str(tmp_path / "source"),
        worktree_path=str(tmp_path / "worktree"),
        base_branch="main",
        working_branch="depbot/test",
        dependency=Dependency(
            name="toydep", current_version="1.0.0", target_version="2.0.0",
            package_manager=PackageManagerName.CONAN, manifest_path="conanfile.txt",
        ),
        artifacts_dir=str(tmp_path / "artifacts"),
        status=JobStatus.FAILED_BUILD,
        status_reason="CMake configure failed (exit code 1)",
    )


def test_command_runner_captures_output_and_exit_status(tmp_path: Path):
    result = run_command(
        command_label="unit.success", args=[sys.executable, "-c", "print('captured')"],
        cwd=tmp_path, artifacts_dir=tmp_path / "logs", timeout_seconds=10,
    )
    assert result.succeeded
    assert "captured" in result.stdout_tail
    assert Path(result.stdout_path).is_file()

    failed = run_command(
        command_label="unit.fail", args=[sys.executable, "-c", "raise SystemExit(7)"],
        cwd=tmp_path, artifacts_dir=tmp_path / "logs", timeout_seconds=10,
    )
    assert failed.exit_code == 7
    assert not failed.succeeded


def test_command_runner_times_out_and_caps_logs(tmp_path: Path):
    timeout = run_command(
        command_label="unit.timeout", args=[sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path, artifacts_dir=tmp_path / "logs", timeout_seconds=0.1,
    )
    assert timeout.timed_out
    assert timeout.exit_code is None
    assert not timeout.succeeded

    noisy = run_command(
        command_label="unit.noisy", args=[sys.executable, "-c", "print('x' * 1200000)"],
        cwd=tmp_path, artifacts_dir=tmp_path / "logs", timeout_seconds=10,
    )
    assert noisy.output_truncated
    assert Path(noisy.stdout_path).stat().st_size <= MAX_LOG_BYTES
    assert len(noisy.stdout_tail) <= 8_000


def test_missing_cmake_executable_is_an_environment_failure(tmp_path: Path):
    missing = run_command(
        command_label="upgraded.configure",
        args=[str(tmp_path / "cmake-does-not-exist.exe"), "-S", "."],
        cwd=tmp_path,
        artifacts_dir=tmp_path / "logs",
        timeout_seconds=5,
    )
    result = VerificationResult(
        phase=VerificationPhase.UPGRADED,
        configure=missing,
        passed=False,
    )
    assert not result.passed
    assert classify_failure(result) == "environment"


def test_evidence_report_discloses_failed_build_and_missing_tests(tmp_path: Path):
    state = _state(tmp_path)
    state.verification = VerificationResult(
        phase=VerificationPhase.UPGRADED,
        dependency_install=CommandResult(
            command_label="upgraded.dependency_install", args=["conan", "install"],
            cwd=state.worktree_path, exit_code=0, duration_seconds=0.2,
        ),
        configure=CommandResult(
            command_label="upgraded.configure", args=["cmake", "-S", "."],
            cwd=state.worktree_path, exit_code=1, duration_seconds=0.3,
            stderr_tail="CMake Error: no compiler was found",
        ),
        failure_category="configuration",
    )
    evidence = collect_evidence(state)
    assert evidence["verification"]["upgraded"]["passed"] is False
    assert evidence["verification"]["upgraded"]["tests"] is None
    report_path = write_repository_report(state=state, artifacts_dir=tmp_path / "artifacts")
    report = report_path.read_text(encoding="utf-8")
    evidence_json = json.loads((tmp_path / "artifacts" / "evidence.json").read_text(encoding="utf-8"))
    assert "Upgraded verification passed: **False**" in report
    assert "ctest: **not run**; not recorded" in report.lower()
    assert evidence_json["remaining_risks"]


def _pattern(status=MigrationVerification.VERIFIED):
    return MigrationPattern(
        pattern_id="toydep-old-sum-v2",
        dependency_name="toydep",
        version_range="<2.0.0 -> 2.0.0",
        old_symbols=["old_sum"],
        replacement_summary="Use sum.",
        evidence_urls=["file:///release-notes.md"],
        verification_status=status,
        source_run_id="run-123",
    )


def test_knowledge_store_only_records_and_returns_verified_patterns(tmp_path: Path):
    store = tmp_path / "patterns.jsonl"
    with pytest.raises(KnowledgeStoreError):
        store_verified_pattern(store, _pattern(MigrationVerification.PARTIAL))
    store_verified_pattern(store, _pattern())
    store_verified_pattern(store, _pattern())  # duplicate IDs are idempotent
    matches = find_verified_patterns(store, dependency_name="toydep", old_symbol="old_sum")
    assert [item.pattern_id for item in matches] == ["toydep-old-sum-v2"]
    assert not find_verified_patterns(store, dependency_name="other")


def test_knowledge_store_rejects_corrupt_json(tmp_path: Path):
    store = tmp_path / "patterns.jsonl"
    store.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(KnowledgeStoreError):
        find_verified_patterns(store, dependency_name="toydep")


def test_repair_proposes_only_new_evidence_backed_removed_symbol_diff(tmp_path: Path):
    state = _state(tmp_path)
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "main.cpp").write_text("int value = old_sum(1, 2);\n", encoding="utf-8")
    change = BreakingChange(
        id="toydep-2-BC1", title="old_sum removed", change_type=ChangeType.REMOVED,
        affected_symbols=["old_sum"], replacement_symbols=["sum"],
        migration_advice="Use sum.", evidence_urls=["file:///release-notes.md"], confidence=0.99,
    )
    usage = CodeUsage(
        symbol="old_sum", file_path="src/main.cpp", line_start=1, line_end=1,
        snippet="int value = old_sum(1, 2);", analysis_method=AnalysisMethod.RIPGREP,
        impact=ImpactLevel.DEFINITE, related_change_ids=[change.id], confidence=0.99,
    )
    state.breaking_changes = [change]
    state.usages = [usage]
    state.migration_plan = plan_migration(
        dependency=state.dependency, changes=[change], usages=[usage]
    )
    failure = VerificationResult(
        phase=VerificationPhase.UPGRADED,
        build=CommandResult(
            command_label="upgraded.build", args=["cmake", "--build", "build"],
            cwd=str(worktree), exit_code=2, duration_seconds=0.1,
            stderr_tail="error: 'old_sum' was not declared in this scope",
        ),
        passed=False,
    )
    proposals = propose_repair(worktree=worktree, state=state, failure=failure, attempt=1)
    assert len(proposals) == 1
    assert "sum(1, 2)" in proposals[0].unified_diff
    state.patches = proposals
    assert not propose_repair(worktree=worktree, state=state, failure=failure, attempt=2)
    assert not propose_repair(worktree=worktree, state=state, failure=failure, attempt=3)


def test_repair_declines_test_regressions(tmp_path: Path):
    state = _state(tmp_path)
    failure = VerificationResult(
        phase=VerificationPhase.UPGRADED,
        tests=CommandResult(
            command_label="upgraded.tests", args=["ctest"], cwd=state.worktree_path,
            exit_code=1, duration_seconds=0.1, stderr_tail="1 test failed",
        ),
        tests_failed=1,
        passed=False,
    )
    assert not propose_repair(
        worktree=Path(state.worktree_path), state=state, failure=failure, attempt=1
    )


def test_upgrade_report_contains_only_recorded_pr_policy_repair_and_test_evidence(tmp_path: Path):
    state = _state(tmp_path)
    state.repo_facts["pull_request"] = {
        "published": False,
        "publish_note": "local PR artifact only; not published to GitHub",
    }
    state.policy_decisions = [{"allowed": True, "reasons": ["recorded decision"]}]
    state.repair_attempt_details = [{
        "attempt": 1,
        "patches": [{
            "patch_id": "repair-1",
            "paths": ["src/main.cpp"],
            "artifact_path": str(Path(state.artifacts_dir) / "patches" / "repair-1.diff"),
            "status": "applied",
            "rejection_reason": None,
        }],
    }]
    baseline_commands = [
        CommandResult(command_label=f"baseline.{stage}", args=[stage], cwd=state.worktree_path,
                      exit_code=0, duration_seconds=0.01)
        for stage in ("install", "configure", "build", "tests")
    ]
    state.baseline = VerificationResult(
        phase=VerificationPhase.BASELINE,
        dependency_install=baseline_commands[0],
        configure=baseline_commands[1],
        build=baseline_commands[2],
        tests=baseline_commands[3],
        tests_passed=2,
        tests_failed=0,
        passed=True,
    )
    state.verification = VerificationResult(
        phase=VerificationPhase.UPGRADED,
        dependency_install=CommandResult(command_label="upgraded.install", args=["install"],
                                         cwd=state.worktree_path, exit_code=0, duration_seconds=0.01),
        configure=CommandResult(command_label="upgraded.configure", args=["configure"],
                                cwd=state.worktree_path, exit_code=0, duration_seconds=0.01),
        build=CommandResult(command_label="upgraded.build", args=["build"],
                            cwd=state.worktree_path, exit_code=0, duration_seconds=0.01),
        tests=CommandResult(command_label="upgraded.tests", args=["ctest"],
                            cwd=state.worktree_path, exit_code=8, duration_seconds=0.01),
        tests_passed=1,
        tests_failed=1,
        failed_test_names=["regression_case"],
        passed=False,
    )

    report_path = write_repository_report(state=state, artifacts_dir=tmp_path / "artifacts")
    report = report_path.read_text(encoding="utf-8")
    evidence = json.loads((tmp_path / "artifacts" / "evidence.json").read_text(encoding="utf-8"))

    assert report_path.name == "upgrade-report.md"
    assert (tmp_path / "artifacts" / "repository-report.md").is_file()
    assert "Published to GitHub: **False**" in report
    assert "local PR artifact only; not published to GitHub" in report
    assert '"allowed": true' in report
    assert "src/main.cpp" in report and "Repair attempt 1" in report
    assert str(Path(state.artifacts_dir) / "patches" / "repair-1.diff") in report
    assert "| Baseline | True | exit 0 | 2 passed, 0 failed | none |" in report
    assert "| Upgraded | False | exit 8 | 1 passed, 1 failed | regression_case |" in report
    assert evidence["policy_decisions"] == state.policy_decisions
    assert evidence["pull_request"] == state.repo_facts["pull_request"]
    assert evidence["repair_attempt_details"] == state.repair_attempt_details

def test_verification_includes_adapter_cmake_configure_args_and_minimal_env(tmp_path: Path, monkeypatch):
    import depbot.verification.pipeline as pipeline_module

    captured: list[tuple[str, list[str], dict[str, str] | None]] = []
    install_envs: list[dict[str, str] | None] = []

    def successful_step(*, label, args, worktree, extra_env, **_kwargs):
        captured.append((label, args, extra_env))
        output = "1 test passed out of 1" if label.endswith(".tests") else ""
        return CommandResult(
            command_label=label,
            args=args,
            cwd=str(worktree),
            exit_code=0,
            duration_seconds=0.01,
            stdout_tail=output,
        )

    original_run_command = pipeline_module.run_command

    def recording_run_command(**kwargs):
        install_envs.append(kwargs.get("extra_env"))
        return original_run_command(**kwargs)

    monkeypatch.setattr(pipeline_module, "_run_step", successful_step)
    monkeypatch.setattr(pipeline_module, "run_command", recording_run_command)
    adapter = SimpleNamespace(
        command_env=lambda: {"CONAN_HOME": str(tmp_path / "isolated-conan")},
        install_args=lambda *_args, **_kwargs: [sys.executable, "-c", "pass"],
        cmake_preset=lambda **_kwargs: None,
        cmake_configure_args=lambda _root, **_kwargs: [
            "-DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake"
        ],
    )
    expected_env = minimal_env(adapter.command_env())

    result = run_verification(
        worktree=tmp_path,
        adapter=adapter,
        phase=VerificationPhase.UPGRADED,
        policy=UpgradePolicy(require_tests=True),
        artifacts_dir=tmp_path / "artifacts",
    )

    configure_args = next(args for label, args, _env in captured if label.endswith(".configure"))
    assert result.passed
    assert "-DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake" in configure_args
    assert install_envs == [expected_env, expected_env]
    assert all(env == expected_env for _label, _args, env in captured)


def test_verification_recreates_build_dir_before_each_phase(tmp_path: Path, monkeypatch):
    captured = []

    def successful_step(*, label, args, worktree, **_kwargs):
        output = "1 test passed out of 1" if label.endswith(".tests") else ""
        return CommandResult(
            command_label=label,
            args=args,
            cwd=str(worktree),
            exit_code=0,
            duration_seconds=0.01,
            stdout_tail=output,
        )

    def install_args(root, **_kwargs):
        captured.append((root / "build" / "stale-cache.txt").exists())
        (root / "build" / "generated-toolchain.txt").write_text("fresh", encoding="utf-8")
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr("depbot.verification.pipeline._run_step", successful_step)
    adapter = SimpleNamespace(
        command_env=lambda: {},
        install_args=install_args,
        cmake_preset=lambda **_kwargs: None,
        cmake_configure_args=lambda _root, **_kwargs: [],
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "stale-cache.txt").write_text("stale", encoding="utf-8")

    for phase in (VerificationPhase.BASELINE, VerificationPhase.UPGRADED):
        result = run_verification(
            worktree=tmp_path,
            adapter=adapter,
            phase=phase,
            policy=UpgradePolicy(require_tests=True),
            artifacts_dir=tmp_path / "artifacts",
        )
        assert result.passed
        (build_dir / "stale-cache.txt").write_text("stale", encoding="utf-8")

    assert captured == [False, False, False, False]


def test_ctest_failure_names_are_extracted_from_recorded_output(tmp_path: Path):
    result = CommandResult(
        command_label="upgraded.tests",
        args=["ctest"],
        cwd=str(tmp_path),
        exit_code=8,
        duration_seconds=0.01,
        stdout_tail=(
            "0% tests passed, 1 tests failed out of 1\n\n"
            "The following tests FAILED:\n  1 - repo_a_test (Failed)\n"
        ),
    )

    assert _test_counts(result) == (0, 1, ["repo_a_test"])

