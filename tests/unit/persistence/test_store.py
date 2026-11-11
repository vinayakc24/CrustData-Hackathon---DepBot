"""SQLite store: transactional updates, idempotency, leases, recovery, side effects."""

import sqlite3
import threading

import pytest

from depbot.models import (
    FleetClassification,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryDependencyRecord,
    RepositoryRecord,
    RepositoryUpgradeJob,
    RepositoryUpgradeOutcome,
    UpgradePolicy,
)
from depbot.persistence.observer import JobSideEffects
from depbot.persistence.store import DuplicateJob, JobEntry, LeaseLost, Store, StoreError, job_key
from depbot.state import DepBotState, JobStatus
from depbot.models import Dependency

EVENT = ReleaseEvent(event_id="e1", dependency_name="toydep", upstream_repo="u",
                     previous_version="1.0.0", target_version="2.0.0")


def _job(repo="repo_a"):
    return RepositoryUpgradeJob(
        job_id=f"e1:{repo}", event=EVENT,
        repository=RepositoryRecord(repository_id=repo, repo_url_or_path=repo, display_name=repo),
        dependency_record=RepositoryDependencyRecord(
            repository_id=repo, dependency_name="toydep", current_version="1.0.0",
            package_manager="conan", manifest_path="conanfile.txt"),
        policy=UpgradePolicy(), repo_root=f"/src/{repo}",
    )


def _outcome(repo="repo_a", status=OutcomeStatus.VERIFIED):
    return RepositoryUpgradeOutcome(repository_id=repo, status=status, verification_passed=status == "verified",
                                    classification=FleetClassification.AFFECTED_CANDIDATE)


def _state(status=JobStatus.INITIALIZED):
    return DepBotState(
        run_id="r1", repository_id="repo_a", original_repo="/src", worktree_path="/w", base_branch="b",
        working_branch="wb", artifacts_dir="/a", status=status,
        dependency=Dependency(name="toydep", current_version="1.0.0", target_version="2.0.0",
                              package_manager="conan", manifest_path="conanfile.txt"),
    )


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "depbot.db", clock=clock)
    s.record_event(EVENT)
    return s


def _run(store, run_id="run1", repos=("repo_a",)):
    return store.create_fleet_run(run_id, "e1", "fleet", "/art/" + run_id, {},
                                  [JobEntry(repository_id=r, job=_job(r), duplicate_outcome=_outcome(r, OutcomeStatus.BLOCKED))
                                   for r in repos])


def test_event_recording_is_idempotent_across_connections(tmp_path):
    first, second = Store(tmp_path / "d.db"), Store(tmp_path / "d.db")
    assert first.record_event(EVENT) is True
    assert second.record_event(EVENT) is False
    assert second.record_event(EVENT.model_copy(update={"event_id": "other-id"})) is False  # same dedup key
    assert [e.event_id for e in first.events()] == ["e1"]
    assert second.find_duplicate_event(EVENT.model_copy(update={"event_id": "x"})).event_id == "e1"


def test_fleet_run_creation_is_all_or_nothing(store):
    entries = [JobEntry(repository_id="repo_a", job=_job("repo_a")), JobEntry(repository_id="broken")]
    with pytest.raises(StoreError):
        store.create_fleet_run("run1", "e1", "fleet", "/art", {}, entries)
    assert store.fleet_run("run1") is None and store.jobs("run1") == []


def test_fleet_run_must_reference_a_recorded_event(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.create_fleet_run("run1", "never-recorded", "fleet", "/art", {}, [])


def test_duplicate_active_job_is_prevented_across_fleet_runs(store):
    assert _run(store, "run1") == {"repo_a": "queued"}
    assert _run(store, "run2") == {"repo_a": "duplicate"}
    dup = store.job(job_key("run2", "repo_a"))
    assert dup.status == "completed" and dup.outcome.status == OutcomeStatus.BLOCKED
    assert "run1:repo_a" in dup.outcome.reason
    with pytest.raises(DuplicateJob):
        store.create_fleet_run("run3", "e1", "fleet", "/art", {}, [JobEntry(repository_id="repo_a", job=_job())])
    assert store.fleet_run("run3") is None
    # Once the first job finishes, the repository may be processed again.
    store.claim("run1:repo_a", "w1")
    store.complete_job("run1:repo_a", "w1", _outcome())
    assert _run(store, "run4") == {"repo_a": "queued"}


def test_only_one_worker_can_claim_a_job(store):
    _run(store)
    winners = []
    barrier = threading.Barrier(8)

    def claim(i):
        barrier.wait()
        if store.claim("run1:repo_a", f"w{i}") is not None:
            winners.append(i)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and store.job("run1:repo_a").attempt == 1


def test_checkpoint_stores_state_and_transition_atomically(store):
    _run(store)
    store.claim("run1:repo_a", "w1")
    state = _state()
    state.transition(JobStatus.INSPECTED, "ok")
    store.checkpoint("run1:repo_a", "w1", state, state.history[-1])
    row = store.job("run1:repo_a")
    assert row.stage == "INSPECTED" and row.state.status == JobStatus.INSPECTED and row.run_id == "r1"
    assert [(t["from_status"], t["to_status"], t["attempt"]) for t in store.transitions("run1:repo_a")] == [
        ("INITIALIZED", "INSPECTED", 1)]


def test_writes_from_a_worker_without_the_lease_are_rejected(store, clock):
    _run(store)
    store.claim("run1:repo_a", "w1", lease_seconds=10)
    clock.now += 11
    assert store.recover_stale("run1") == ["run1:repo_a"]
    assert store.claim("run1:repo_a", "w2").attempt == 2
    state = _state()
    for write in (lambda: store.checkpoint("run1:repo_a", "w1", state),
                  lambda: store.heartbeat("run1:repo_a", "w1"),
                  lambda: store.complete_job("run1:repo_a", "w1", _outcome())):
        with pytest.raises(LeaseLost):
            write()
    assert store.job("run1:repo_a").worker_id == "w2" and store.job("run1:repo_a").outcome is None


def test_live_leases_are_not_recovered_and_heartbeats_extend_them(store, clock):
    _run(store)
    store.claim("run1:repo_a", "w1", lease_seconds=10)
    clock.now += 8
    store.heartbeat("run1:repo_a", "w1", lease_seconds=10)
    clock.now += 8
    assert store.recover_stale("run1") == []
    assert store.job("run1:repo_a").status == "running"


def test_job_is_abandoned_after_max_attempts_instead_of_looping(store, clock):
    _run(store)
    for attempt in (1, 2):
        store.claim("run1:repo_a", "w", lease_seconds=1)
        clock.now += 5
        assert store.recover_stale("run1", max_attempts=2) == (["run1:repo_a"] if attempt == 1 else [])
    row = store.job("run1:repo_a")
    assert row.status == "abandoned" and "abandoned after 2 attempts" in row.error
    assert [r["action"] for r in store.recovery_log("run1:repo_a")] == ["requeued", "abandoned"]


def test_cancel_marks_queued_jobs_and_flags_the_run(store):
    _run(store, repos=("repo_a", "repo_b"))
    store.claim("run1:repo_a", "w1")
    assert store.request_cancel("run1") == 1
    assert store.cancel_requested("run1")
    assert store.job("run1:repo_b").status == "cancelled" and store.job("run1:repo_a").status == "running"
    assert store.claim("run1:repo_b", "w2") is None
    with pytest.raises(StoreError):
        store.request_cancel("no-such-run")


def test_artifact_metadata_is_indexed_but_content_stays_on_disk(store, tmp_path):
    _run(store)
    report = tmp_path / "report.md"
    report.write_text("hello")
    store.add_artifact("run1:repo_a", "report", report)
    store.add_artifact("run1:repo_a", "report", report)  # idempotent per path
    store.add_artifact("run1:repo_a", "report", tmp_path / "missing.md")  # ignored
    (meta,) = store.artifacts("run1:repo_a")
    assert meta["size_bytes"] == 5 and meta["path"] == str(report)
    assert meta["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert (tmp_path / "depbot.db").stat().st_size < 200_000


def test_side_effect_ledger_distinguishes_pending_from_done(store):
    _run(store)
    ledger = JobSideEffects(store, "run1:repo_a")
    key = "github_pr:acme/app:depbot/toydep-2.0.0"
    assert ledger.completed(key) is None
    ledger.begin(key, "github_pr")
    ledger.begin(key, "github_pr")  # a retry re-declares intent
    assert ledger.completed(key) is None  # crashed before completion: not treated as done
    assert store.side_effects()[0]["status"] == "pending"
    ledger.complete(key, {"url": "https://github.com/acme/app/pull/1"})
    assert Store(store.path).side_effect_result(key) == {"url": "https://github.com/acme/app/pull/1"}


def test_newer_schema_is_refused(tmp_path):
    Store(tmp_path / "d.db").close()
    conn = sqlite3.connect(tmp_path / "d.db")
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="schema v999"):
        Store(tmp_path / "d.db")
