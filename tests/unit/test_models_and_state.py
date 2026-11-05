import pytest
from pydantic import ValidationError

from depbot.models import (
    BreakingChange,
    ChangeType,
    CommandResult,
    Dependency,
    FleetClassification,
    FleetRunSummary,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryUpgradeOutcome,
    UpgradePolicy,
    VerificationPhase,
    VerificationResult,
)
from depbot.state import DepBotState, InvalidTransition, JobStatus


def _ok(label: str) -> CommandResult:
    return CommandResult(command_label=label, args=["x"], cwd="/w", exit_code=0, duration_seconds=0.1)


def _state(**kw) -> DepBotState:
    dep = Dependency(
        name="toydep", current_version="1.0.0", target_version="2.0.0",
        package_manager="conan", manifest_path="conanfile.txt",
    )
    return DepBotState(
        run_id="r1", repository_id="repo_a", original_repo="/src", worktree_path="/w",
        base_branch="main", working_branch="depbot/toydep-2.0.0", dependency=dep,
        artifacts_dir="/a", **kw,
    )


def test_breaking_change_requires_evidence_unless_unknown():
    with pytest.raises(ValidationError):
        BreakingChange(
            id="bc1", title="t", change_type="removed", affected_symbols=["old_sum"],
            migration_advice="use sum", evidence_urls=[], confidence=0.9,
        )
    unknown = BreakingChange(
        id="bc2", title="t", change_type=ChangeType.UNKNOWN, affected_symbols=[],
        migration_advice="unknown", evidence_urls=[], confidence=0.1,
    )
    assert unknown.change_type == "unknown"


def test_confidence_bounds_and_extra_fields_rejected():
    with pytest.raises(ValidationError):
        BreakingChange(
            id="b", title="t", change_type="removed", affected_symbols=[], migration_advice="",
            evidence_urls=["file://x"], confidence=1.5,
        )
    with pytest.raises(ValidationError):
        ReleaseEvent(event_id="e", dependency_name="d", upstream_repo="u", target_version="2", bogus=1)


def test_verification_passed_requires_real_successful_steps():
    with pytest.raises(ValidationError):
        VerificationResult(phase=VerificationPhase.UPGRADED, passed=True)
    failed_build = _ok("build").model_copy(update={"exit_code": 2})
    with pytest.raises(ValidationError):
        VerificationResult(
            phase="upgraded", dependency_install=_ok("i"), configure=_ok("c"),
            build=failed_build, passed=True,
        )
    ok = VerificationResult(
        phase="upgraded", dependency_install=_ok("i"), configure=_ok("c"), build=_ok("b"),
        tests=_ok("t"), passed=True,
    )
    assert ok.passed


def test_outcome_verified_needs_passed_flag_and_summary_counts_are_derived():
    with pytest.raises(ValidationError):
        RepositoryUpgradeOutcome(
            repository_id="a", status="verified", classification="affected_candidate"
        )
    outcomes = [
        RepositoryUpgradeOutcome(repository_id="a", status=OutcomeStatus.VERIFIED,
                                 classification=FleetClassification.AFFECTED_CANDIDATE,
                                 verification_passed=True),
        RepositoryUpgradeOutcome(repository_id="b", status=OutcomeStatus.FAILED,
                                 classification=FleetClassification.AFFECTED_CANDIDATE),
        RepositoryUpgradeOutcome(repository_id="c", status=OutcomeStatus.SKIPPED,
                                 classification=FleetClassification.ALREADY_AT_TARGET_OR_NEWER),
    ]
    event = ReleaseEvent(event_id="e", dependency_name="toydep", upstream_repo="local",
                         previous_version="1.0.0", target_version="2.0.0")
    s = FleetRunSummary.from_outcomes(event, outcomes)
    assert (s.repositories_considered, s.repositories_affected, s.repositories_skipped,
            s.repositories_verified, s.repositories_failed_or_blocked) == (3, 2, 1, 1, 1)


def test_policy_forbids_auto_merge():
    with pytest.raises(ValidationError):
        UpgradePolicy(auto_merge=True)


def test_state_happy_path_and_invalid_transition():
    st = _state()
    for s in (JobStatus.INSPECTED, JobStatus.RESEARCHED, JobStatus.IMPACT_ANALYZED,
              JobStatus.PLANNED, JobStatus.PATCHED, JobStatus.VERIFIED):
        st.transition(s)
    assert st.status == JobStatus.VERIFIED and st.is_terminal
    assert [h.to_status for h in st.history][-1] == JobStatus.VERIFIED
    with pytest.raises(InvalidTransition):
        _state().transition(JobStatus.VERIFIED)


def test_repair_loop_is_bounded():
    st = _state(max_repair_attempts=1)
    for s in (JobStatus.INSPECTED, JobStatus.RESEARCHED, JobStatus.IMPACT_ANALYZED,
              JobStatus.PLANNED, JobStatus.PATCHED, JobStatus.FAILED_BUILD):
        st.transition(s)
    assert not st.is_terminal  # one repair left
    st.transition(JobStatus.PLANNED)
    assert st.repair_attempts == 1
    st.transition(JobStatus.PATCHED)
    st.transition(JobStatus.FAILED_BUILD)
    assert st.is_terminal
    with pytest.raises(InvalidTransition):
        st.transition(JobStatus.PLANNED)
