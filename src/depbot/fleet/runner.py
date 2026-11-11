"""Fleet orchestrator: one ReleaseEvent → selection → isolated jobs → fleet summary.

Owner: Member A. Decides *which* repositories run; ``depbot.orchestrator``
decides *how* one repository is upgraded. A crash in one job becomes that
repository's FAILED outcome and never stops the rest of the fleet.

The run is durable: the event, the fleet run and every job live in SQLite
(``depbot.persistence.store``). Jobs execute on a bounded worker pool under
leases; ``resume_fleet`` re-queues jobs whose worker died and continues each from
its last stage checkpoint. The summary is always derived from stored outcomes.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from depbot.execution import (
    CancelToken,
    ExecutionBackend,
    ExecutionConfig,
    ExecutionContext,
    build_backend,
)
from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.events import EventLog
from depbot.fleet.pool import WorkerPool, effective_workers
from depbot.fleet.registry import Fleet
from depbot.fleet.report import render_fleet_markdown
from depbot.fleet.selection import RepositorySelection, select_repositories
from depbot.interfaces import PipelineStages
from depbot.llm.runtime import LLMFactory
from depbot.models import (
    FleetClassification,
    FleetRunSummary,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryUpgradeJob,
    RepositoryUpgradeOutcome,
)
from depbot.orchestrator import RepositoryJobRunner
from depbot.persistence.observer import DurableJobObserver, JobSideEffects
from depbot.persistence.store import (
    ABANDONED,
    ACTIVE,
    CANCELLED,
    DEFAULT_LEASE_SECONDS,
    QUEUED,
    RUNNING,
    JobEntry,
    JobRow,
    Store,
)

log = logging.getLogger(__name__)


class DuplicateEvent(RuntimeError):
    pass


class FleetRunError(RuntimeError):
    """The fleet run cannot start or resume (unknown run, unusable execution backend...)."""


def _job_for(
    event: ReleaseEvent, sel: RepositorySelection, fleet: Fleet, create_pr: bool
) -> RepositoryUpgradeJob:
    assert sel.dependency_record is not None and sel.repo_root is not None
    return RepositoryUpgradeJob(
        job_id=f"{event.event_id}:{sel.repository.repository_id}",
        event=event,
        repository=sel.repository,
        dependency_record=sel.dependency_record,
        policy=fleet.policy_for(sel.repository),
        repo_root=sel.repo_root,
        create_pr=create_pr,
    )


def _job_outcome(job: RepositoryUpgradeJob, status: OutcomeStatus, reason: str) -> RepositoryUpgradeOutcome:
    """Outcome for a job that produced none itself (crash, duplicate, cancelled, abandoned)."""
    return RepositoryUpgradeOutcome(
        repository_id=job.repository.repository_id,
        status=status,
        classification=FleetClassification.AFFECTED_CANDIDATE,
        reason=reason,
        dependency_name=job.event.dependency_name,
        from_version=job.dependency_record.current_version,
        to_version=job.event.target_version,
    )


def _entry(event: ReleaseEvent, sel: RepositorySelection, fleet: Fleet, create_pr: bool) -> JobEntry:
    repository_id = sel.repository.repository_id
    if not sel.eligible:
        return JobEntry(repository_id=repository_id, outcome=sel.to_outcome(event))
    try:
        job = _job_for(event, sel, fleet, create_pr)
    except Exception as exc:  # isolate: never let one repository stop the fleet
        log.exception("could not build the job for %s", repository_id)
        return JobEntry(
            repository_id=repository_id,
            error=f"{type(exc).__name__}: {exc}",
            outcome=RepositoryUpgradeOutcome(
                repository_id=repository_id,
                status=OutcomeStatus.FAILED,
                classification=sel.classification,
                reason=f"job crashed before producing an outcome: {type(exc).__name__}: {exc}",
                dependency_name=event.dependency_name,
                from_version=sel.dependency_record.current_version if sel.dependency_record else None,
                to_version=event.target_version,
            ),
        )
    return JobEntry(repository_id=repository_id, job=job,
                    duplicate_outcome=_job_outcome(job, OutcomeStatus.BLOCKED, "duplicate job prevented"))


class _FleetExecution:
    """Everything the workers of one fleet run share."""

    def __init__(self, store: Store, fleet_run_id: str, stages: PipelineStages, artifacts_root: Path,
                 llm_factory: LLMFactory | None, backend: ExecutionBackend, cancel: threading.Event,
                 lease_seconds: float) -> None:
        self.store, self.fleet_run_id, self.stages = store, fleet_run_id, stages
        self.artifacts_root, self.llm_factory, self.backend = artifacts_root, llm_factory, backend
        self.cancel, self.lease_seconds = cancel, lease_seconds
        self.install_lock = threading.Lock()
        self.process = f"{socket.gethostname()}-{os.getpid()}"

    def cancel_requested(self) -> bool:
        return self.cancel.is_set() or self.store.cancel_requested(self.fleet_run_id)

    def run_job(self, key: str, worker: str) -> None:
        if self.cancel_requested():
            return  # stays queued; finalisation records it as cancelled
        worker_id = f"{self.process}-{worker}"
        row = self.store.claim(key, worker_id, self.lease_seconds)
        if row is None or row.job is None:
            return  # claimed elsewhere, cancelled or already finished
        job = row.job
        token = CancelToken(probe=lambda: self.store.cancel_requested(self.fleet_run_id), linked=self.cancel)
        context = ExecutionContext(backend=self.backend, cancel=token, install_lock=self.install_lock)
        with DurableJobObserver(self.store, key, worker_id, token, attempt=row.attempt,
                                lease_seconds=self.lease_seconds) as observer:
            try:
                runner = RepositoryJobRunner(
                    job, self.stages, self.artifacts_root, self.llm_factory,
                    observer=observer, execution=context, ledger=JobSideEffects(self.store, key),
                    resume_state=row.state if row.attempt > 1 else None,
                    attempt=row.attempt, fleet_run_id=self.fleet_run_id,
                )
                if row.attempt > 1:
                    action = "resumed" if runner.resumed_from is not None else "restarted"
                    self.store.log_recovery(key, action, f"attempt {row.attempt}: {runner.resume_note}")
                log.info("running job for %s (attempt %d)", job.repository.repository_id, row.attempt)
                runner.run()
            except Exception as exc:  # isolate: never let one repository stop the fleet
                log.exception("fleet job for %s crashed", job.repository.repository_id)
                if observer.completed or observer.lease_lost:
                    return
                reason = f"job crashed before producing an outcome: {type(exc).__name__}: {exc}"
                self.store.complete_job(key, worker_id, _job_outcome(job, OutcomeStatus.FAILED, reason),
                                        error=reason)


def _drain(shared: _FleetExecution, workers: int) -> None:
    """Run every queued job of the fleet run on the bounded pool."""
    queued = [row.job_key for row in shared.store.jobs(shared.fleet_run_id) if row.status == QUEUED]
    if not queued:
        return
    with WorkerPool(shared.run_job, workers=min(workers, len(queued)), name="job") as pool:
        for key in queued:
            if shared.cancel_requested():
                pool.cancel()
                break
            pool.submit(key)


def _unfinished_outcome(row: JobRow) -> RepositoryUpgradeOutcome:
    job = row.job
    if row.status == CANCELLED:
        status, reason = OutcomeStatus.BLOCKED, f"cancelled: {row.error or 'fleet run cancelled'}"
    elif row.status == ABANDONED:
        status, reason = OutcomeStatus.FAILED, row.error or "abandoned after repeated worker failures"
    elif row.status == RUNNING:
        status, reason = OutcomeStatus.FAILED, (
            f"interrupted at stage {row.stage or 'INITIALIZED'} (worker {row.worker_id}); "
            f"resume with: depbot runs resume {row.fleet_run_id}")
    else:
        status, reason = OutcomeStatus.BLOCKED, f"not started; resume with: depbot runs resume {row.fleet_run_id}"
    if job is not None:
        return _job_outcome(job, status, reason)
    return RepositoryUpgradeOutcome(repository_id=row.repository_id, status=status,
                                    classification=FleetClassification.AFFECTED_CANDIDATE, reason=reason)


def _finalize(store: Store, fleet_run_id: str, event: ReleaseEvent, fleet_id: str) -> FleetRunSummary:
    """Write the fleet summary from stored outcomes; close the run when nothing is pending."""
    if store.cancel_requested(fleet_run_id):
        store.request_cancel(fleet_run_id)  # idempotent: marks still-queued jobs cancelled
    rows = store.jobs(fleet_run_id)
    outcomes = [row.outcome or _unfinished_outcome(row) for row in rows]
    run = store.fleet_run(fleet_run_id)
    assert run is not None
    fleet_dir = Path(run["artifacts_dir"])
    fleet_dir.mkdir(parents=True, exist_ok=True)
    summary = FleetRunSummary.from_outcomes(event, outcomes)
    summary = summary.model_copy(update={"summary_path": str(fleet_dir / "summary.md")})
    (fleet_dir / "summary.json").write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (fleet_dir / "summary.md").write_text(render_fleet_markdown(summary, fleet_id), encoding="utf-8")
    if not any(row.status in ACTIVE for row in rows):
        status = "cancelled" if store.cancel_requested(fleet_run_id) else "completed"
        store.finish_fleet_run(fleet_run_id, status, summary.summary_path)
    return summary


def _usable_backend(execution: ExecutionConfig) -> ExecutionBackend:
    backend = build_backend(execution)
    problems = backend.problems()
    if problems:
        raise FleetRunError(f"execution backend '{execution.backend}' is unusable: " + "; ".join(problems))
    return backend


def run_fleet(
    event: ReleaseEvent,
    fleet: Fleet,
    *,
    stages: PipelineStages,
    state_dir: Path,
    artifacts_root: Path,
    force: bool = False,
    create_pr: bool = False,
    llm_factory: LLMFactory | None = None,
    workers: int = 1,
    execution: ExecutionConfig | None = None,
    cancel: threading.Event | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    store: Store | None = None,
) -> FleetRunSummary:
    """Process one release event across the fleet.

    ``llm_factory`` is called once per job so every job gets its own LLM client.
    ``workers`` bounds concurrent jobs (further capped by the host's CPU and memory
    relative to the per-job execution limits). ``cancel`` stops the run cooperatively.
    """
    execution = execution or ExecutionConfig()
    backend = _usable_backend(execution)  # before anything is recorded: a misconfigured run leaves no trace
    store = store or Store.for_state_dir(state_dir)
    events = EventLog(state_dir / "events.json", store)
    duplicate = events.find_duplicate(event)
    if duplicate is not None and not force:
        unfinished = store.fleet_runs(event_id=duplicate.event_id, unfinished=True)
        hint = (f"; fleet run {unfinished[-1]['fleet_run_id']} is unfinished: resume it with "
                f"`depbot runs resume`" if unfinished else "; use --force to reprocess")
        raise DuplicateEvent(f"event already processed as {duplicate.event_id!r}{hint}")
    if duplicate is None and not events.record(event):  # lost a race with another process
        raise DuplicateEvent(f"event {event.event_id!r} was recorded concurrently by another run")
    recorded_event_id = duplicate.event_id if duplicate is not None else event.event_id

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fleet_run_id = f"fleet-{event.event_id}-{stamp}-{secrets.token_hex(3)}"
    fleet_dir = (artifacts_root / fleet_run_id).resolve()
    fleet_dir.mkdir(parents=True, exist_ok=False)

    selections = select_repositories(
        event, fleet, DependencyIndex(state_dir / "inventory.json"), sources_dir=state_dir / "sources"
    )
    (fleet_dir / "selection.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in selections], indent=2) + "\n", encoding="utf-8"
    )
    workers = effective_workers(workers, execution.limits)
    store.create_fleet_run(
        fleet_run_id, recorded_event_id, fleet.fleet_id, fleet_dir,
        {"workers": workers, "create_pr": create_pr, "execution": execution.model_dump(mode="json"),
         "artifacts_root": str(Path(artifacts_root).resolve()), "event": event.model_dump(mode="json")},
        [_entry(event, sel, fleet, create_pr) for sel in selections],
    )
    shared = _FleetExecution(store, fleet_run_id, stages, artifacts_root, llm_factory, backend,
                             cancel or threading.Event(), lease_seconds)
    try:
        _drain(shared, workers)
    finally:
        if shared.cancel.is_set():
            store.request_cancel(fleet_run_id)
    return _finalize(store, fleet_run_id, event, fleet.fleet_id)


def resume_fleet(
    fleet_run_id: str,
    *,
    stages: PipelineStages,
    state_dir: Path,
    llm_factory: LLMFactory | None = None,
    workers: int | None = None,
    execution: ExecutionConfig | None = None,
    cancel: threading.Event | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    store: Store | None = None,
) -> FleetRunSummary:
    """Continue an interrupted fleet run.

    Jobs whose worker lease expired are re-queued (or abandoned after too many
    attempts) and every queued job runs; each continues from its last stage
    checkpoint when the working copy still matches it, otherwise from scratch.
    Jobs still held by a live worker are left alone and reported as unfinished.
    """
    store = store or Store.for_state_dir(state_dir)
    run = store.fleet_run(fleet_run_id)
    if run is None:
        raise FleetRunError(f"unknown fleet run: {fleet_run_id}")
    if run["cancel_requested"]:
        raise FleetRunError(f"fleet run {fleet_run_id} was cancelled; start a new run with --force")
    config = json.loads(run["config"])
    execution = execution or ExecutionConfig.model_validate(config.get("execution") or {})
    event = ReleaseEvent.model_validate(config["event"])
    store.recover_stale(fleet_run_id)
    shared = _FleetExecution(store, fleet_run_id, stages, Path(config["artifacts_root"]), llm_factory,
                             _usable_backend(execution), cancel or threading.Event(), lease_seconds)
    try:
        _drain(shared, effective_workers(workers or int(config.get("workers", 1)), execution.limits))
    finally:
        if shared.cancel.is_set():
            store.request_cancel(fleet_run_id)
    return _finalize(store, fleet_run_id, event, run["fleet_id"])
