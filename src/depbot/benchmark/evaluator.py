"""Score one finished job against a case's ground truth, and aggregate case results.

Every number is computed from the job's recorded state and the files it left on
disk. A case that could not run contributes to no metric.
"""

from __future__ import annotations

from pathlib import Path

from depbot.benchmark.models import Aggregate, BenchmarkCase, CaseResult, Counts
from depbot.models import ImpactLevel, OutcomeStatus, RepositoryUpgradeOutcome
from depbot.state import DepBotState, JobStatus
from depbot.telemetry import JobTelemetry


def _norm(symbol: str) -> str:
    """Compare API names without namespace qualification or a parameter list."""
    return symbol.split("(", 1)[0].strip().rsplit("::", 1)[-1]


def _change_key(affected: list[str], replacement: list[str]) -> tuple[frozenset[str], frozenset[str]]:
    return frozenset(_norm(s) for s in affected), frozenset(_norm(s) for s in replacement)


def _score(expected: set, predicted: set) -> Counts:
    return Counts(tp=len(expected & predicted), fp=len(predicted - expected), fn=len(expected - predicted))


def _patch_problems(case: BenchmarkCase, state: DepBotState) -> list[str]:
    worktree = Path(state.worktree_path)
    managed = {state.dependency.manifest_path, "conan.lock", "vcpkg.json", "vcpkg-configuration.json"}
    edited = {path for path in state.changed_files if path not in managed}
    wanted = set(case.expected.patched_lines) | set(case.expected.absent_text)
    problems = [f"unexpected source edit: {p}" for p in sorted(edited - wanted)]
    problems += [f"expected edit missing: {p}" for p in sorted(wanted - edited)]
    for rel in sorted(wanted):
        try:
            text = (worktree / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{rel}: unreadable ({exc})")
            continue
        lines = text.splitlines()
        for want in case.expected.patched_lines.get(rel, []):
            got = lines[want.line - 1].rstrip() if want.line <= len(lines) else "<missing line>"
            if got != want.text.rstrip():
                problems.append(f"{rel}:{want.line}: expected {want.text.rstrip()!r}, found {got!r}")
        for gone in case.expected.absent_text.get(rel, []):
            if gone in text:
                problems.append(f"{rel}: still contains {gone!r}")
    return problems


def evaluate_case(
    case: BenchmarkCase,
    outcome: RepositoryUpgradeOutcome,
    state: DepBotState | None,
    telemetry: JobTelemetry | None,
) -> CaseResult:
    expected = case.expected
    actual = outcome.status
    verified = actual == OutcomeStatus.VERIFIED
    result = CaseResult(
        case_id=case.id, split=case.split, status="completed",
        expected_outcome=expected.outcome.value, actual_outcome=actual.value,
        outcome_reason=outcome.reason,
        outcome_match=actual == expected.outcome or actual in expected.also_acceptable,
        verification_expected=expected.outcome in (OutcomeStatus.VERIFIED, OutcomeStatus.UNAFFECTED),
        abstention_expected=expected.outcome != OutcomeStatus.VERIFIED,
        run_id=outcome.run_id,
    )
    expected_changes = {_change_key(c.affected_symbols, c.replacement_symbols) for c in expected.breaking_changes}
    expected_usages = {(u.file, u.line, _norm(u.symbol)) for u in expected.affected_usages}
    if state is not None:
        result.job_status = state.status.value
        result.artifacts_dir = state.artifacts_dir
        result.verification_passed = bool(state.verification and state.verification.passed)
        reached = {change.to_status for change in state.history}
        result.research_ran = JobStatus.RESEARCHED in reached
        result.impact_ran = JobStatus.IMPACT_ANALYZED in reached
    if state is not None and result.research_ran:
        predicted = {_change_key(c.affected_symbols, c.replacement_symbols)
                     for c in state.breaking_changes if c.affected_symbols}
        result.breaking_changes = _score(expected_changes, predicted)
        types = {_change_key(c.affected_symbols, c.replacement_symbols): c.change_type
                 for c in state.breaking_changes if c.affected_symbols}
        result.change_type_mismatches = sorted(
            f"{'/'.join(sorted(_norm(s) for s in want.affected_symbols))}: expected "
            f"{want.change_type.value}, got {types[key].value}"
            for want in expected.breaking_changes
            if want.change_type is not None
            and (key := _change_key(want.affected_symbols, want.replacement_symbols)) in types
            and types[key] != want.change_type
        )
    if state is not None and result.impact_ran:
        result.impact = _score(expected_usages, {
            (u.file_path, u.line_start, _norm(u.symbol)) for u in state.usages if u.impact == ImpactLevel.DEFINITE
        })

    if expected.outcome == OutcomeStatus.VERIFIED:
        if verified and state is not None:
            result.patch_problems = _patch_problems(case, state)
            result.patch_correct = not result.patch_problems
        else:
            result.patch_correct = False
            result.patch_problems = [f"no verified patch was produced (outcome: {actual.value})"]
    result.abstained = (not verified) if result.abstention_expected else None
    # The one error class that must stay at zero: claiming success that is not real.
    result.false_verified = verified and (expected.outcome != OutcomeStatus.VERIFIED or result.patch_correct is False)
    if telemetry is not None:
        result.duration_seconds = telemetry.duration_seconds
        if telemetry.llm is not None:
            result.llm_calls = telemetry.llm.calls
            result.llm_input_tokens = telemetry.llm.input_tokens
            result.llm_output_tokens = telemetry.llm.output_tokens
            result.llm_cost_usd = telemetry.llm.cost_usd
    return result


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _rounded(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _total(values: list) -> int | float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def aggregate(results: list[CaseResult]) -> Aggregate:
    done = [r for r in results if r.status == "completed"]
    changes = sum((r.breaking_changes for r in done), Counts())
    impact = sum((r.impact for r in done), Counts())
    patched = [r for r in done if r.patch_correct is not None]
    verifiable = [r for r in done if r.verification_expected]
    abstain = [r for r in done if r.abstention_expected]
    cost = _total([r.llm_cost_usd for r in done])
    return Aggregate(
        cases=len(results),
        completed=len(done),
        not_run=sum(r.status == "not_run" for r in results),
        errors=sum(r.status == "error" for r in results),
        breaking_change_precision=_rounded(changes.precision),
        breaking_change_recall=_rounded(changes.recall),
        impact_precision=_rounded(impact.precision),
        impact_recall=_rounded(impact.recall),
        outcome_accuracy=_ratio(sum(bool(r.outcome_match) for r in done), len(done)),
        patch_correctness=_ratio(sum(bool(r.patch_correct) for r in patched), len(patched)),
        verification_success=_ratio(sum(r.verification_passed for r in verifiable), len(verifiable)),
        safe_abstention_accuracy=_ratio(sum(bool(r.abstained) for r in abstain), len(abstain)),
        false_verified=sum(r.false_verified for r in done),
        duration_seconds=round(sum(r.duration_seconds or 0.0 for r in done), 3),
        llm_calls=sum(r.llm_calls for r in done),
        llm_input_tokens=_total([r.llm_input_tokens for r in done]),
        llm_output_tokens=_total([r.llm_output_tokens for r in done]),
        llm_cost_usd=round(cost, 6) if cost is not None else None,
    )
