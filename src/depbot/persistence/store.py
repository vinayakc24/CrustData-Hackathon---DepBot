"""SQLite-backed durable state for fleet orchestration.

One database per fleet state directory (``<state_dir>/depbot.db``). It records
release events, fleet runs, repository jobs with worker leases, every stage
transition and stage attempt, artifact metadata (the artifacts themselves stay on
the filesystem), recovery actions and external side effects.

Every mutation is one ``BEGIN IMMEDIATE`` transaction, so a crash leaves either
the old or the new state, never a mixture. Connections are per thread; WAL mode
lets several workers and processes share the file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from depbot.models import ReleaseEvent, RepositoryUpgradeJob, RepositoryUpgradeOutcome, utc_now_iso
from depbot.state import DepBotState, StatusChange

SCHEMA_VERSION = 1
DB_NAME = "depbot.db"
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_MAX_ATTEMPTS = 3

QUEUED, RUNNING, COMPLETED, CANCELLED, ABANDONED = "queued", "running", "completed", "cancelled", "abandoned"
ACTIVE = (QUEUED, RUNNING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_events (
    event_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_runs (
    fleet_run_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES release_events(event_id),
    fleet_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- running | completed | cancelled
    artifacts_dir TEXT NOT NULL,
    config TEXT NOT NULL,              -- JSON: workers, execution, create_pr, ...
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    summary_path TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_key TEXT PRIMARY KEY,          -- "<fleet_run_id>:<repository_id>"
    fleet_run_id TEXT NOT NULL REFERENCES fleet_runs(fleet_run_id),
    event_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,              -- queued | running | completed | cancelled | abandoned
    stage TEXT,                        -- latest JobStatus of the pipeline
    attempt INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_expires REAL,
    run_id TEXT,
    artifacts_dir TEXT,
    job_json TEXT,                     -- RepositoryUpgradeJob (NULL for selection-only outcomes)
    state_json TEXT,                   -- last durable DepBotState checkpoint
    outcome_json TEXT,
    telemetry_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_event_repo
    ON jobs(event_id, repository_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_by_run ON jobs(fleet_run_id, ordinal);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL REFERENCES jobs(job_key),
    attempt INTEGER NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL REFERENCES jobs(job_key),
    attempt INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL REFERENCES jobs(job_key),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    recorded_at TEXT NOT NULL,
    UNIQUE (job_key, path)
);
CREATE TABLE IF NOT EXISTS recovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS side_effects (
    effect_key TEXT PRIMARY KEY,       -- e.g. "github_pr:acme/app:depbot/fmt-7.0.0"
    kind TEXT NOT NULL,
    job_key TEXT,
    status TEXT NOT NULL,              -- pending | done
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class StoreError(RuntimeError):
    pass


class DuplicateJob(StoreError):
    """An active job already exists for this (event, repository)."""


class LeaseLost(StoreError):
    """The worker no longer owns the job (its lease expired and was taken over)."""


class JobRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_key: str
    fleet_run_id: str
    event_id: str
    repository_id: str
    ordinal: int
    status: str
    stage: str | None = None
    attempt: int = 0
    worker_id: str | None = None
    lease_expires: float | None = None
    run_id: str | None = None
    artifacts_dir: str | None = None
    job_json: str | None = None
    state_json: str | None = None
    outcome_json: str | None = None
    telemetry_json: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str

    @property
    def job(self) -> RepositoryUpgradeJob | None:
        return RepositoryUpgradeJob.model_validate_json(self.job_json) if self.job_json else None

    @property
    def state(self) -> DepBotState | None:
        return DepBotState.model_validate_json(self.state_json) if self.state_json else None

    @property
    def outcome(self) -> RepositoryUpgradeOutcome | None:
        return RepositoryUpgradeOutcome.model_validate_json(self.outcome_json) if self.outcome_json else None

    @property
    def telemetry(self) -> dict[str, Any] | None:
        return json.loads(self.telemetry_json) if self.telemetry_json else None


class JobEntry(BaseModel):
    """One repository of a fleet run: a job to queue, or an outcome that needs no job."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str
    job: RepositoryUpgradeJob | None = None
    outcome: RepositoryUpgradeOutcome | None = None  # recorded as-is when there is no job
    duplicate_outcome: RepositoryUpgradeOutcome | None = None  # used if an active job already exists
    error: str | None = None


def job_key(fleet_run_id: str, repository_id: str) -> str:
    return f"{fleet_run_id}:{repository_id}"


def _dedup(event: ReleaseEvent) -> str:
    return json.dumps(list(event.dedup_key))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Store:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            for statement in filter(str.strip, _SCHEMA.split(";\n")):
                db.execute(statement)
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path} has schema v{row['value']}; this DepBot understands v{SCHEMA_VERSION}"
                )

    @classmethod
    def for_state_dir(cls, state_dir: Path, **kwargs) -> Store:
        return cls(Path(state_dir) / DB_NAME, **kwargs)

    # -- connection handling ------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._connection().execute(sql, params).fetchall()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- release events ---------------------------------------------------------

    def find_duplicate_event(self, event: ReleaseEvent) -> ReleaseEvent | None:
        rows = self._query(
            "SELECT payload FROM release_events WHERE event_id=? OR dedup_key=? LIMIT 1",
            (event.event_id, _dedup(event)),
        )
        return ReleaseEvent.model_validate_json(rows[0]["payload"]) if rows else None

    def record_event(self, event: ReleaseEvent) -> bool:
        """Idempotent: returns False (and stores nothing) when the event is already known."""
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO release_events VALUES (?, ?, ?, ?)",
                (event.event_id, _dedup(event), event.model_dump_json(), utc_now_iso()),
            )
            return cursor.rowcount == 1

    def events(self) -> list[ReleaseEvent]:
        return [ReleaseEvent.model_validate_json(r["payload"])
                for r in self._query("SELECT payload FROM release_events ORDER BY rowid")]

    # -- fleet runs ---------------------------------------------------------------

    def create_fleet_run(self, fleet_run_id: str, event_id: str, fleet_id: str, artifacts_dir: Path,
                         config: dict[str, Any], entries: list[JobEntry] | None = None) -> dict[str, str]:
        """Create the run and all of its job rows in ONE transaction: a crash can never
        leave a run with only part of its fleet enqueued.

        Returns ``{repository_id: "queued" | "outcome" | "duplicate"}``. An entry whose
        (event, repository) already has an active job is not enqueued again; its
        ``duplicate_outcome`` is recorded instead.
        """
        now = utc_now_iso()
        result: dict[str, str] = {}
        with self.transaction() as db:
            db.execute(
                "INSERT INTO fleet_runs (fleet_run_id, event_id, fleet_id, status, artifacts_dir, config, created_at)"
                " VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (fleet_run_id, event_id, fleet_id, str(artifacts_dir), json.dumps(config), now),
            )
            for ordinal, entry in enumerate(entries or []):
                key = job_key(fleet_run_id, entry.repository_id)
                active = db.execute(
                    "SELECT job_key FROM jobs WHERE event_id=? AND repository_id=? AND status IN ('queued','running')",
                    (event_id, entry.repository_id),
                ).fetchone() if entry.job is not None else None
                if entry.job is not None and active is None:
                    db.execute(
                        "INSERT INTO jobs (job_key, fleet_run_id, event_id, repository_id, ordinal, status, job_json,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                        (key, fleet_run_id, event_id, entry.repository_id, ordinal, entry.job.model_dump_json(),
                         now, now),
                    )
                    result[entry.repository_id] = "queued"
                    continue
                outcome, error = entry.outcome, entry.error
                if active is not None:
                    if entry.duplicate_outcome is None:
                        raise DuplicateJob(f"an active job already exists: {active['job_key']}")
                    outcome = entry.duplicate_outcome.model_copy(update={
                        "reason": f"duplicate job prevented: {active['job_key']} is already active for this "
                                  "event and repository"})
                    error = f"duplicate of {active['job_key']}"
                if outcome is None:
                    raise StoreError(f"entry for {entry.repository_id} has neither a job nor an outcome")
                db.execute(
                    "INSERT INTO jobs (job_key, fleet_run_id, event_id, repository_id, ordinal, status, outcome_json,"
                    " error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)",
                    (key, fleet_run_id, event_id, entry.repository_id, ordinal, outcome.model_dump_json(),
                     error, now, now),
                )
                result[entry.repository_id] = "duplicate" if active is not None else "outcome"
        return result

    def fleet_run(self, fleet_run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM fleet_runs WHERE fleet_run_id=?", (fleet_run_id,))
        return dict(rows[0]) if rows else None

    def fleet_runs(self, *, event_id: str | None = None, unfinished: bool = False) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM fleet_runs WHERE 1=1", []
        if event_id is not None:
            sql, params = sql + " AND event_id=?", [*params, event_id]
        if unfinished:
            sql += " AND status='running'"
        return [dict(r) for r in self._query(sql + " ORDER BY created_at, rowid", tuple(params))]

    def finish_fleet_run(self, fleet_run_id: str, status: str, summary_path: str | None) -> None:
        with self.transaction() as db:
            db.execute("UPDATE fleet_runs SET status=?, summary_path=?, finished_at=? WHERE fleet_run_id=?",
                       (status, summary_path, utc_now_iso(), fleet_run_id))

    # -- jobs -----------------------------------------------------------------------

    def job(self, key: str) -> JobRow | None:
        rows = self._query("SELECT * FROM jobs WHERE job_key=?", (key,))
        return JobRow.model_validate(dict(rows[0])) if rows else None

    def jobs(self, fleet_run_id: str) -> list[JobRow]:
        return [JobRow.model_validate(dict(r))
                for r in self._query("SELECT * FROM jobs WHERE fleet_run_id=? ORDER BY ordinal", (fleet_run_id,))]

    def claim(self, key: str, worker_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> JobRow | None:
        """Atomically take a queued job. None if it is not queued (claimed, cancelled, done)."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='running', worker_id=?, lease_expires=?, attempt=attempt+1, updated_at=?"
                " WHERE job_key=? AND status='queued'",
                (worker_id, self.clock() + lease_seconds, utc_now_iso(), key),
            )
            if cursor.rowcount != 1:
                return None
        return self.job(key)

    def _owned(self, db: sqlite3.Connection, key: str, worker_id: str) -> None:
        row = db.execute("SELECT status, worker_id FROM jobs WHERE job_key=?", (key,)).fetchone()
        if row is None or row["status"] != RUNNING or row["worker_id"] != worker_id:
            raise LeaseLost(f"worker {worker_id} no longer owns job {key}")

    def heartbeat(self, key: str, worker_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        with self.transaction() as db:
            self._owned(db, key, worker_id)
            db.execute("UPDATE jobs SET lease_expires=? WHERE job_key=?", (self.clock() + lease_seconds, key))

    def checkpoint(self, key: str, worker_id: str, state: DepBotState, change: StatusChange | None = None,
                   lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        """Persist the job state and (optionally) one transition, atomically."""
        with self.transaction() as db:
            self._owned(db, key, worker_id)
            attempt = db.execute("SELECT attempt FROM jobs WHERE job_key=?", (key,)).fetchone()["attempt"]
            if change is not None:
                db.execute(
                    "INSERT INTO transitions (job_key, attempt, from_status, to_status, note, at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (key, attempt, change.from_status.value, change.to_status.value, change.note, change.at),
                )
            db.execute(
                "UPDATE jobs SET state_json=?, stage=?, run_id=?, artifacts_dir=?, lease_expires=?, updated_at=?"
                " WHERE job_key=?",
                (state.model_dump_json(), state.status.value, state.run_id, state.artifacts_dir,
                 self.clock() + lease_seconds, utc_now_iso(), key),
            )

    def record_stage(self, key: str, *, attempt: int, stage: str, status: str, started_at: str,
                     duration_seconds: float, detail: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO stage_runs (job_key, attempt, stage, status, started_at, duration_seconds, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, attempt, stage, status, started_at, duration_seconds, detail),
            )

    def complete_job(self, key: str, worker_id: str, outcome: RepositoryUpgradeOutcome, *,
                     state: DepBotState | None = None, telemetry: dict[str, Any] | None = None,
                     status: str = COMPLETED, error: str | None = None) -> None:
        with self.transaction() as db:
            self._owned(db, key, worker_id)
            db.execute(
                "UPDATE jobs SET status=?, outcome_json=?, telemetry_json=?, error=?, lease_expires=NULL,"
                " state_json=COALESCE(?, state_json), stage=COALESCE(?, stage), updated_at=? WHERE job_key=?",
                (status, outcome.model_dump_json(), json.dumps(telemetry) if telemetry is not None else None,
                 error, state.model_dump_json() if state else None, state.status.value if state else None,
                 utc_now_iso(), key),
            )

    def add_artifact(self, key: str, kind: str, path: Path) -> None:
        """Record metadata only; the artifact itself stays on the filesystem."""
        path = Path(path)
        if not path.is_file():
            return
        with self.transaction() as db:
            db.execute(
                "INSERT INTO artifacts (job_key, kind, path, sha256, size_bytes, recorded_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(job_key, path) DO UPDATE SET sha256=excluded.sha256, size_bytes=excluded.size_bytes,"
                " kind=excluded.kind, recorded_at=excluded.recorded_at",
                (key, kind, str(path), file_sha256(path), path.stat().st_size, utc_now_iso()),
            )

    def transitions(self, key: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM transitions WHERE job_key=? ORDER BY id", (key,))]

    def stage_runs(self, key: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM stage_runs WHERE job_key=? ORDER BY id", (key,))]

    def artifacts(self, key: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM artifacts WHERE job_key=? ORDER BY id", (key,))]

    def recovery_log(self, key: str | None = None) -> list[dict[str, Any]]:
        if key is None:
            return [dict(r) for r in self._query("SELECT * FROM recovery_log ORDER BY id")]
        return [dict(r) for r in self._query("SELECT * FROM recovery_log WHERE job_key=? ORDER BY id", (key,))]

    # -- cancellation and recovery ----------------------------------------------

    def request_cancel(self, fleet_run_id: str) -> int:
        """Flag the run; queued jobs are cancelled now, running jobs stop at their next check."""
        now = utc_now_iso()
        with self.transaction() as db:
            if db.execute("UPDATE fleet_runs SET cancel_requested=1 WHERE fleet_run_id=?",
                          (fleet_run_id,)).rowcount != 1:
                raise StoreError(f"unknown fleet run: {fleet_run_id}")
            queued = db.execute("SELECT job_key FROM jobs WHERE fleet_run_id=? AND status='queued'",
                                (fleet_run_id,)).fetchall()
            for row in queued:
                db.execute("UPDATE jobs SET status='cancelled', error='cancelled before start', updated_at=?"
                           " WHERE job_key=?", (now, row["job_key"]))
                db.execute("INSERT INTO recovery_log (job_key, at, action, detail) VALUES (?, ?, 'cancelled', ?)",
                           (row["job_key"], now, "cancelled while queued"))
            return len(queued)

    def cancel_requested(self, fleet_run_id: str) -> bool:
        rows = self._query("SELECT cancel_requested FROM fleet_runs WHERE fleet_run_id=?", (fleet_run_id,))
        return bool(rows and rows[0]["cancel_requested"])

    def recover_stale(self, fleet_run_id: str | None = None, *,
                      max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[str]:
        """Re-queue running jobs whose lease expired (their worker crashed or hung).

        A job that already used ``max_attempts`` is marked ``abandoned`` instead, so a
        job that reliably kills its worker cannot loop forever. Returns re-queued keys.
        """
        now, stamp = self.clock(), utc_now_iso()
        requeued: list[str] = []
        with self.transaction() as db:
            sql = "SELECT job_key, attempt, worker_id, stage FROM jobs WHERE status='running' AND lease_expires < ?"
            params: list[Any] = [now]
            if fleet_run_id is not None:
                sql, params = sql + " AND fleet_run_id=?", [*params, fleet_run_id]
            for row in db.execute(sql, tuple(params)).fetchall():
                detail = (f"lease of worker {row['worker_id']} expired at stage {row['stage'] or 'INITIALIZED'} "
                          f"(attempt {row['attempt']})")
                if row["attempt"] >= max_attempts:
                    db.execute(
                        "UPDATE jobs SET status='abandoned', worker_id=NULL, lease_expires=NULL, error=?,"
                        " updated_at=? WHERE job_key=?",
                        (f"abandoned after {row['attempt']} attempts; {detail}", stamp, row["job_key"]),
                    )
                    action = "abandoned"
                else:
                    db.execute(
                        "UPDATE jobs SET status='queued', worker_id=NULL, lease_expires=NULL, updated_at=?"
                        " WHERE job_key=?", (stamp, row["job_key"]),
                    )
                    action = "requeued"
                    requeued.append(row["job_key"])
                db.execute("INSERT INTO recovery_log (job_key, at, action, detail) VALUES (?, ?, ?, ?)",
                           (row["job_key"], stamp, action, detail))
        return requeued

    def log_recovery(self, key: str, action: str, detail: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO recovery_log (job_key, at, action, detail) VALUES (?, ?, ?, ?)",
                       (key, utc_now_iso(), action, detail))

    # -- external side effects ---------------------------------------------------

    def side_effect_result(self, effect_key: str) -> dict[str, Any] | None:
        """The recorded result of a side effect that already happened, else None."""
        rows = self._query("SELECT result FROM side_effects WHERE effect_key=? AND status='done'", (effect_key,))
        return json.loads(rows[0]["result"]) if rows else None

    def begin_side_effect(self, effect_key: str, kind: str, job_key_: str | None) -> None:
        """Record intent before acting, so a crash mid-effect is visible on recovery."""
        now = utc_now_iso()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO side_effects (effect_key, kind, job_key, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?) ON CONFLICT(effect_key) DO UPDATE SET updated_at=excluded.updated_at",
                (effect_key, kind, job_key_, now, now),
            )

    def complete_side_effect(self, effect_key: str, result: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute("UPDATE side_effects SET status='done', result=?, updated_at=? WHERE effect_key=?",
                       (json.dumps(result), utc_now_iso(), effect_key))

    def side_effects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM side_effects ORDER BY created_at, rowid")]
