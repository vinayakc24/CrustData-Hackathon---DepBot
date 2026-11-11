"""Bridges one running job to the durable store: checkpoints, stage runs, heartbeats,
artifact metadata and the side-effect ledger."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from depbot.execution import CancelToken
from depbot.models import RepositoryUpgradeOutcome
from depbot.persistence.store import CANCELLED, COMPLETED, DEFAULT_LEASE_SECONDS, LeaseLost, Store
from depbot.state import DepBotState, StatusChange
from depbot.telemetry import JobTelemetry, StageSpan

log = logging.getLogger(__name__)

# Evidence files whose metadata (path, size, sha256) is indexed; contents stay on disk.
_ARTIFACT_GLOBS = {
    "state": ["state.json"],
    "telemetry": ["telemetry.json"],
    "report": ["upgrade-report.md", "*.md"],
    "evidence": ["inspection.json", "breaking_changes.json", "impact.json", "plan.json",
                 "api_diff.json", "impact_layers.json"],
    "patch": ["patches/*.diff"],
    "pull_request": ["pr/pr.json", "pr/pr-body.md"],
    "verification": ["verification/*/configuration-matrix.json"],
}


class DurableJobObserver:
    """``JobObserver`` writing to SQLite. Never raises into the pipeline: if the
    worker's lease was taken over, it cancels the job instead (another worker owns it)."""

    def __init__(self, store: Store, job_key: str, worker_id: str, cancel: CancelToken, *,
                 attempt: int, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.store, self.job_key, self.worker_id = store, job_key, worker_id
        self.cancel, self.attempt, self.lease_seconds = cancel, attempt, lease_seconds
        self.lease_lost = False
        self.completed = False
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None

    # -- lease heartbeat (long builds outlive a single lease period) ---------------

    def __enter__(self) -> DurableJobObserver:
        self._beat = threading.Thread(target=self._heartbeat_loop, daemon=True, name=f"lease-{self.job_key}")
        self._beat.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=5)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(max(self.lease_seconds / 3, 0.05)):
            if not self._guard(lambda: self.store.heartbeat(self.job_key, self.worker_id, self.lease_seconds)):
                return

    def _guard(self, write) -> bool:
        if self.lease_lost:
            return False
        try:
            write()
            return True
        except LeaseLost as exc:
            self.lease_lost = True
            self.cancel.cancel(f"lease lost: {exc}")
            log.warning("%s", exc)
            return False

    # -- JobObserver -----------------------------------------------------------------

    def checkpoint(self, state: DepBotState, change: StatusChange | None) -> None:
        self._guard(lambda: self.store.checkpoint(self.job_key, self.worker_id, state, change, self.lease_seconds))

    def stage_finished(self, span: StageSpan) -> None:
        if not self.lease_lost:
            self.store.record_stage(
                self.job_key, attempt=span.attempt, stage=span.stage, status=span.status,
                started_at=span.started_at, duration_seconds=span.duration_seconds, detail=span.detail,
            )

    def finished(self, state: DepBotState, outcome: RepositoryUpgradeOutcome, telemetry: JobTelemetry) -> None:
        if self.lease_lost:
            return
        root = Path(state.artifacts_dir)
        for kind, patterns in _ARTIFACT_GLOBS.items():
            for pattern in patterns:
                for path in sorted(root.glob(pattern)):
                    self.store.add_artifact(self.job_key, kind, path)
        cancelled = self.cancel.is_set()
        self.completed = self._guard(lambda: self.store.complete_job(
            self.job_key, self.worker_id, outcome, state=state, telemetry=telemetry.model_dump(mode="json"),
            status=CANCELLED if cancelled else COMPLETED,
            error=self.cancel.reason if cancelled else None,
        ))


class JobSideEffects:
    """``SideEffectLedger`` for one job, backed by the store."""

    def __init__(self, store: Store, job_key: str) -> None:
        self.store, self.job_key = store, job_key

    def completed(self, key: str) -> dict | None:
        return self.store.side_effect_result(key)

    def begin(self, key: str, kind: str) -> None:
        self.store.begin_side_effect(key, kind, self.job_key)

    def complete(self, key: str, result: dict) -> None:
        self.store.complete_side_effect(key, result)
