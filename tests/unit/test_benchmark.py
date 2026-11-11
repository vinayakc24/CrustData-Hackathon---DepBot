"""Benchmark registry, evaluator, report and runner harness (no real builds here)."""

import json
import subprocess

import pytest

from depbot.benchmark.evaluator import aggregate, evaluate_case
from depbot.benchmark.models import (
    BenchmarkCase,
    BenchmarkError,
    BenchmarkRegistry,
    BenchmarkResults,
    CaseResult,
    Counts,
    load_registry,
)
from depbot.benchmark.report import render_markdown
from depbot.benchmark.runner import BenchmarkRunner, tree_sha256
from depbot.cli import main
from depbot.models import (
    BreakingChange,
    CodeUsage,
    CommandResult,
    Dependency,
    FleetClassification,
    OutcomeStatus,
    RepositoryUpgradeOutcome,
    VerificationResult,
)
from depbot.state import DepBotState, JobStatus
from tests.unit.test_orchestrator import MAIN, _stages

REGISTRY = "benchmarks/registry.yaml"


def _case(**expected):
    expected.setdefault("outcome", "verified")
    return BenchmarkCase(
        id="c1", description="d", repository={"path": "repo", "tree_sha256": "x"}, dependency="toydep",
        baseline_version="1.0.0", target_version="2.0.0", expected=expected,
    )


VERIFIED_CASE = dict(
    breaking_changes=[{"affected_symbols": ["toydep::old_sum(int, int)"], "replacement_symbols": ["sum"],
                       "change_type": "removed"}],
    affected_usages=[{"file": "src/main.cpp", "line": 2, "symbol": "old_sum"}],
    patched_lines={"src/main.cpp": [{"line": 2, "text": "int main() { return sum(2, 3); }"}]},
)


def _ok(label):
    return CommandResult(command_label=label, args=[label], cwd="/w", exit_code=0, duration_seconds=0.1)


def _state(tmp_path, *, status=JobStatus.VERIFIED, line="int main() { return sum(2, 3); }", changed=("conanfile.txt", "src/main.cpp"),
           changes=None, usages=None, reached=(JobStatus.INSPECTED, JobStatus.RESEARCHED, JobStatus.IMPACT_ANALYZED)):
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "main.cpp").write_text(f"#include <x>\n{line}\n")
    state = DepBotState(
        run_id="r", repository_id="c1", original_repo="/src", worktree_path=str(worktree), base_branch="b",
        working_branch="w", artifacts_dir=str(tmp_path),
        dependency=Dependency(name="toydep", current_version="1.0.0", target_version="2.0.0",
                              package_manager="conan", manifest_path="conanfile.txt"),
        changed_files=list(changed),
        breaking_changes=changes if changes is not None else [BreakingChange(
            id="BC1", title="t", change_type="removed", affected_symbols=["old_sum"], replacement_symbols=["sum"],
            migration_advice="a", evidence_urls=["file:///n.md"], confidence=0.9)],
        usages=usages if usages is not None else [CodeUsage(
            symbol="old_sum", file_path="src/main.cpp", line_start=2, line_end=2, snippet="s",
            analysis_method="ripgrep", impact="definite", related_change_ids=["BC1"], confidence=0.9)],
        verification=VerificationResult(phase="upgraded", dependency_install=_ok("i"), configure=_ok("c"),
                                        build=_ok("b"), tests=_ok("t"), passed=True),
    )
    previous = JobStatus.INITIALIZED
    for nxt in reached:
        state.transition(nxt)
        previous = nxt
    state.status = status
    return state


def _outcome(status):
    return RepositoryUpgradeOutcome(repository_id="c1", status=status, verification_passed=status == "verified",
                                    classification=FleetClassification.AFFECTED_CANDIDATE)


# ------------------------------------------------------------------- evaluator


def test_correct_migration_scores_perfectly(tmp_path):
    result = evaluate_case(_case(**VERIFIED_CASE), _outcome("verified"), _state(tmp_path), None)
    assert result.outcome_match and result.patch_correct and not result.false_verified
    assert result.breaking_changes == Counts(tp=1) and result.impact == Counts(tp=1)
    assert result.verification_passed and result.abstained is None and not result.change_type_mismatches


def test_verified_with_a_wrong_patch_is_a_false_verified(tmp_path):
    state = _state(tmp_path, line="int main() { return sum(3, 2); }")
    result = evaluate_case(_case(**VERIFIED_CASE), _outcome("verified"), state, None)
    assert result.patch_correct is False and result.false_verified
    assert "expected 'int main() { return sum(2, 3); }'" in result.patch_problems[0]


def test_unexpected_or_missing_source_edits_make_the_patch_wrong(tmp_path):
    extra = _state(tmp_path, changed=("conanfile.txt", "src/main.cpp", "src/other.cpp"))
    assert "unexpected source edit: src/other.cpp" in evaluate_case(
        _case(**VERIFIED_CASE), _outcome("verified"), extra, None).patch_problems
    missing = _state(tmp_path, changed=("conanfile.txt",))
    result = evaluate_case(_case(**VERIFIED_CASE), _outcome("verified"), missing, None)
    assert "expected edit missing: src/main.cpp" in result.patch_problems and result.false_verified


def test_verified_when_abstention_was_correct_is_a_false_verified(tmp_path):
    result = evaluate_case(_case(outcome="blocked"), _outcome("verified"), _state(tmp_path), None)
    assert result.false_verified and result.abstained is False and not result.outcome_match


def test_safe_abstention_and_acceptable_alternatives(tmp_path):
    case = _case(outcome="failed", also_acceptable=["blocked"])
    blocked = evaluate_case(case, _outcome("blocked"), _state(tmp_path, status=JobStatus.BLOCKED), None)
    assert blocked.outcome_match and blocked.abstained and not blocked.false_verified and blocked.patch_correct is None
    unaffected = evaluate_case(case, _outcome("unaffected"), _state(tmp_path), None)
    assert not unaffected.outcome_match and unaffected.abstained  # wrong label, but still not a false claim


def test_failing_to_migrate_is_a_miss_not_a_false_verified(tmp_path):
    result = evaluate_case(_case(**VERIFIED_CASE), _outcome("failed"), _state(tmp_path, status=JobStatus.FAILED_BUILD), None)
    assert not result.outcome_match and result.patch_correct is False and not result.false_verified


def test_precision_and_recall_count_wrong_and_missing_predictions(tmp_path):
    wrong = BreakingChange(id="BC2", title="t", change_type="removed", affected_symbols=["product"],
                           replacement_symbols=["mul"], migration_advice="a", evidence_urls=["file:///n"], confidence=0.9)
    stray = CodeUsage(symbol="old_sum", file_path="src/main.cpp", line_start=9, line_end=9, snippet="s",
                      analysis_method="ripgrep", impact="definite", related_change_ids=["BC2"], confidence=0.9)
    possible = stray.model_copy(update={"impact": "possible", "line_start": 2, "line_end": 2})
    result = evaluate_case(_case(**VERIFIED_CASE), _outcome("failed"),
                           _state(tmp_path, changes=[wrong], usages=[stray, possible]), None)
    assert result.breaking_changes == Counts(tp=0, fp=1, fn=1)
    assert result.impact == Counts(tp=0, fp=1, fn=1)  # 'possible' usages are not predictions


def test_stages_that_never_ran_are_not_scored(tmp_path):
    state = _state(tmp_path, status=JobStatus.BLOCKED, reached=(), changes=[], usages=[])
    result = evaluate_case(_case(outcome="blocked", breaking_changes=VERIFIED_CASE["breaking_changes"]),
                           _outcome("blocked"), state, None)
    assert not result.research_ran and result.breaking_changes == Counts()
    assert aggregate([result]).breaking_change_recall is None


def test_change_type_disagreement_is_reported_separately(tmp_path):
    case = _case(**{**VERIFIED_CASE, "breaking_changes": [
        {"affected_symbols": ["old_sum"], "replacement_symbols": ["sum"], "change_type": "renamed"}]})
    result = evaluate_case(case, _outcome("verified"), _state(tmp_path), None)
    assert result.breaking_changes == Counts(tp=1)
    assert result.change_type_mismatches == ["old_sum: expected renamed, got removed"]


def test_aggregate_ignores_cases_that_did_not_run(tmp_path):
    good = evaluate_case(_case(**VERIFIED_CASE), _outcome("verified"), _state(tmp_path), None)
    bad = evaluate_case(_case(outcome="blocked"), _outcome("verified"), _state(tmp_path), None)
    skipped = CaseResult(case_id="x", split="dev", status="not_run", reason="missing requirement: conan",
                         expected_outcome="verified")
    total = aggregate([good, bad, skipped])
    assert (total.cases, total.completed, total.not_run) == (3, 2, 1)
    assert total.outcome_accuracy == 0.5 and total.false_verified == 1
    assert total.patch_correctness == 1.0 and total.safe_abstention_accuracy == 0.0
    assert total.llm_cost_usd is None and total.llm_input_tokens is None
    empty = aggregate([skipped])
    assert empty.outcome_accuracy is None and empty.breaking_change_precision is None


# ------------------------------------------------------------ registry / report


def test_shipped_registry_is_valid_and_every_fixture_matches_its_pin():
    from pathlib import Path

    registry = load_registry(Path(REGISTRY))
    assert len({c.id for c in registry.cases}) == len(registry.cases) >= 15
    assert {c.split for c in registry.cases} == {"dev", "held_out"}
    for case in registry.cases:
        root = (Path(REGISTRY).parent / case.repository.path).resolve()
        assert tree_sha256(root) == case.repository.tree_sha256, f"{case.id}: fixture drifted from its pin"
        for recipe in case.setup.conan_recipes:
            assert (Path(REGISTRY).parent / recipe / "conanfile.py").is_file()
        assert case.changelog is None or (Path(REGISTRY).parent / case.changelog).is_file()


@pytest.mark.parametrize("patch,message", [
    ({"repository": {"path": "r"}}, "pinned with tree_sha256"),
    ({"repository": {"url": "https://example.com/r.git"}}, "pinned with commit"),
    ({"repository": {"path": "r", "tree_sha256": "x", "url": "u", "commit": "c"}}, "exactly one"),
    ({"expected": {"outcome": "blocked", "patched_lines": {"a.cpp": [{"line": 1, "text": "x"}]}}}, "may expect patched"),
    ({"expected": {"outcome": "failed", "also_acceptable": ["verified"]}}, "cannot be a merely acceptable"),
    ({"expected": {"outcome": "verified", "typo_field": 1}}, "typo_field"),
])
def test_registry_rejects_unpinned_or_inconsistent_cases(tmp_path, patch, message):
    case = {"id": "c1", "description": "d", "repository": {"path": "r", "tree_sha256": "x"}, "dependency": "d",
            "baseline_version": "1", "target_version": "2", "expected": {"outcome": "verified"}, **patch}
    path = tmp_path / "registry.yaml"
    path.write_text(json.dumps({"schema_version": 1, "suite": "s", "suite_version": "1", "cases": [case]}))
    with pytest.raises(BenchmarkError, match=message):
        load_registry(path)
    with pytest.raises(ValueError, match="duplicate case id"):
        BenchmarkRegistry(schema_version=1, suite="s", suite_version="1", cases=[_case(), _case()])


def test_report_lists_unrun_cases_without_scoring_them(tmp_path):
    good = evaluate_case(_case(**VERIFIED_CASE), _outcome("verified"), _state(tmp_path), None)
    skipped = CaseResult(case_id="needs-net", split="held_out", status="not_run",
                         reason="missing requirement: network", expected_outcome="verified")
    results = BenchmarkResults(
        suite="s", suite_version="1", generated_at="now", depbot_version="0", splits_run=["dev", "held_out"],
        toolchain={"cmake": "cmake 3"}, execution={"backend": "local", "image": None, "image_digest": None},
        aggregate={"overall": aggregate([good, skipped]), "dev": aggregate([good]), "held_out": aggregate([skipped])},
        cases=[good, skipped],
    )
    text = render_markdown(results)
    assert "| Cases (completed / not run / error) | 1 / 1 / 0 | 1 / 0 / 0 | 0 / 1 / 0 |" in text
    assert "**NOT_RUN**" in text and "missing requirement: network" in text
    assert "| Outcome accuracy | 100.0% | 100.0% | n/a |" in text


# --------------------------------------------------------------------- runner


def _fixture_repo(root, version="1.0.0"):
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.cpp").write_text(MAIN)
    (root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\nproject(x CXX)\n")
    (root / "conanfile.txt").write_text(f"[requires]\ntoydep/{version}\n")


def _registry(tmp_path, **case):
    spec = {"id": "c1", "description": "d", "dependency": "toydep", "baseline_version": "1.0.0",
            "target_version": "2.0.0", "requires": ["git"],
            "expected": {"outcome": "verified", "breaking_changes": [{"affected_symbols": ["old_sum"],
                                                                      "replacement_symbols": ["sum"]}],
                         "affected_usages": [{"file": "src/main.cpp", "line": 2, "symbol": "old_sum"}],
                         "patched_lines": {"src/main.cpp": [{"line": 2, "text": "int main() { return sum(2, 3) == 5 ? 0 : 1; }"}]}},
            **case}
    path = tmp_path / "registry.yaml"
    path.write_text(json.dumps({"schema_version": 1, "suite": "t", "suite_version": "1", "cases": [spec]}))
    return path


def _runner(path, tmp_path, **kw):
    return BenchmarkRunner(load_registry(path), path, tmp_path / "out", stages_factory=_stages, **kw)


def test_runner_drives_the_real_engine_and_writes_json_and_markdown(tmp_path):
    _fixture_repo(tmp_path / "repo")
    path = _registry(tmp_path, repository={"path": "repo", "tree_sha256": tree_sha256(tmp_path / "repo")})
    results = _runner(path, tmp_path).run(splits=["dev"])
    (case,) = results.cases
    assert case.status == "completed" and case.outcome_match and case.patch_correct and not case.false_verified
    assert case.duration_seconds is not None and case.job_status == "VERIFIED"
    saved = json.loads((tmp_path / "out" / "results.json").read_text())
    assert saved["aggregate"]["overall"]["outcome_accuracy"] == 1.0 and saved["suite_version"] == "1"
    assert saved["toolchain"]["python"] and saved["execution"]["backend"] == "local"
    assert "| c1 | dev | verified | verified |" in (tmp_path / "out" / "report.md").read_text()
    assert not (tmp_path / "repo" / ".git").exists(), "the pinned fixture itself must never be touched"


def test_drifted_fixture_is_not_run(tmp_path):
    _fixture_repo(tmp_path / "repo")
    path = _registry(tmp_path, repository={"path": "repo", "tree_sha256": tree_sha256(tmp_path / "repo")})
    (tmp_path / "repo" / "src" / "main.cpp").write_text(MAIN + "// edited\n")
    (case,) = _runner(path, tmp_path).run(splits=["dev"]).cases
    assert case.status == "not_run" and "fixture drifted" in case.reason


def test_missing_requirement_is_reported_not_scored(tmp_path, monkeypatch):
    _fixture_repo(tmp_path / "repo")
    path = _registry(tmp_path, requires=["git", "network"],
                     repository={"path": "repo", "tree_sha256": tree_sha256(tmp_path / "repo")})
    monkeypatch.delenv("DEPBOT_BENCH_NETWORK", raising=False)
    results = _runner(path, tmp_path).run(splits=["dev"])
    assert results.cases[0].status == "not_run" and "network" in results.cases[0].reason
    assert results.aggregate["overall"].completed == 0 and results.aggregate["overall"].outcome_accuracy is None


def test_git_url_repository_is_checked_out_at_the_pinned_commit(tmp_path):
    origin = tmp_path / "origin"
    _fixture_repo(origin)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=origin, check=True)
    subprocess.run([*git, "add", "-A"], cwd=origin, check=True)
    subprocess.run([*git, "commit", "-qm", "v1"], cwd=origin, check=True)
    pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True).stdout.strip()
    (origin / "src" / "main.cpp").write_text("int main() { return 0; }\n")  # a later upstream commit
    subprocess.run([*git, "commit", "-qam", "v2"], cwd=origin, check=True)
    path = _registry(tmp_path, repository={"url": str(origin), "commit": pinned})
    (case,) = _runner(path, tmp_path).run(splits=["dev"]).cases
    assert case.status == "completed" and case.outcome_match and case.patch_correct, case


def test_harness_crash_is_an_error_not_a_result(tmp_path):
    _fixture_repo(tmp_path / "repo")
    path = _registry(tmp_path, repository={"path": "repo", "tree_sha256": tree_sha256(tmp_path / "repo")})

    def broken_stages():
        raise RuntimeError("stage loader exploded")

    runner = BenchmarkRunner(load_registry(path), path, tmp_path / "out", stages_factory=broken_stages)
    results = runner.run(splits=["dev"])
    assert results.cases[0].status == "error" and results.aggregate["overall"].errors == 1


def test_held_out_cases_run_only_when_requested(tmp_path):
    _fixture_repo(tmp_path / "repo")
    path = _registry(tmp_path, split="held_out",
                     repository={"path": "repo", "tree_sha256": tree_sha256(tmp_path / "repo")})
    assert _runner(path, tmp_path).run(splits=["dev"]).cases == []
    results = _runner(path, tmp_path).run(splits=["dev", "held_out"])
    assert [c.split for c in results.cases] == ["held_out"] and results.aggregate["held_out"].completed == 1


def test_bench_cli_list_hash_and_unknown_case(tmp_path, capsys):
    assert main(["bench", "list", "--registry", REGISTRY]) == 0
    out = capsys.readouterr().out
    assert "header-relocation" in out and "held_out" in out
    _fixture_repo(tmp_path / "repo")
    assert main(["bench", "hash", str(tmp_path / "repo")]) == 0
    assert capsys.readouterr().out.split()[0] == tree_sha256(tmp_path / "repo")
    assert main(["bench", "run", "--registry", REGISTRY, "--case", "nope", "--out", str(tmp_path / "o")]) == 2
    assert "unknown case id" in capsys.readouterr().err
