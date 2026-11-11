"""Run inspection and comparison over the durable store (``depbot runs ...``)."""

from __future__ import annotations

from typing import Any

from depbot.persistence.store import JobRow, Store


class UnknownRun(LookupError):
    pass


def _fleet_run(store: Store, fleet_run_id: str) -> dict[str, Any]:
    run = store.fleet_run(fleet_run_id)
    if run is None:
        matches = [r for r in store.fleet_runs() if r["fleet_run_id"].startswith(fleet_run_id)]
        if len(matches) != 1:
            raise UnknownRun(f"unknown fleet run: {fleet_run_id}" if not matches
                             else f"ambiguous fleet run prefix: {fleet_run_id}")
        run = matches[0]
    return run


def list_runs(store: Store) -> list[dict[str, Any]]:
    rows = []
    for run in store.fleet_runs():
        counts: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for job in store.jobs(run["fleet_run_id"]):
            counts[job.status] = counts.get(job.status, 0) + 1
            if job.outcome is not None:
                outcomes[job.outcome.status.value] = outcomes.get(job.outcome.status.value, 0) + 1
        rows.append({
            "fleet_run_id": run["fleet_run_id"], "event_id": run["event_id"], "fleet_id": run["fleet_id"],
            "status": run["status"], "created_at": run["created_at"], "finished_at": run["finished_at"],
            "cancel_requested": bool(run["cancel_requested"]), "jobs": counts, "outcomes": outcomes,
        })
    return rows


def _job_summary(store: Store, job: JobRow, *, detail: bool) -> dict[str, Any]:
    telemetry = job.telemetry or {}
    outcome = job.outcome
    data: dict[str, Any] = {
        "repository_id": job.repository_id,
        "job_key": job.job_key,
        "status": job.status,
        "stage": job.stage,
        "attempt": job.attempt,
        "run_id": job.run_id,
        "artifacts_dir": job.artifacts_dir,
        "outcome": outcome.status.value if outcome else None,
        "reason": outcome.reason if outcome else job.error,
        "pr_url": outcome.pr_url if outcome else None,
        "duration_seconds": telemetry.get("duration_seconds"),
        "repair_attempts": telemetry.get("repair_attempts"),
        "resumed_from": telemetry.get("resumed_from"),
        "stages": [
            {"stage": s["stage"], "attempt": s["attempt"], "status": s["status"],
             "duration_seconds": s["duration_seconds"], "detail": s["detail"]}
            for s in store.stage_runs(job.job_key)
        ],
        "recovery": [{"at": r["at"], "action": r["action"], "detail": r["detail"]}
                     for r in store.recovery_log(job.job_key)],
    }
    if detail:
        data["telemetry"] = telemetry or None
        data["transitions"] = [
            {"attempt": t["attempt"], "from": t["from_status"], "to": t["to_status"], "note": t["note"], "at": t["at"]}
            for t in store.transitions(job.job_key)
        ]
        data["artifacts"] = [
            {"kind": a["kind"], "path": a["path"], "sha256": a["sha256"], "size_bytes": a["size_bytes"]}
            for a in store.artifacts(job.job_key)
        ]
    return data


def show_run(store: Store, fleet_run_id: str, *, repository_id: str | None = None) -> dict[str, Any]:
    run = _fleet_run(store, fleet_run_id)
    jobs = store.jobs(run["fleet_run_id"])
    if repository_id is not None:
        jobs = [j for j in jobs if j.repository_id == repository_id]
        if not jobs:
            raise UnknownRun(f"fleet run {run['fleet_run_id']} has no repository {repository_id!r}")
    return {
        "fleet_run_id": run["fleet_run_id"], "event_id": run["event_id"], "fleet_id": run["fleet_id"],
        "status": run["status"], "created_at": run["created_at"], "finished_at": run["finished_at"],
        "cancel_requested": bool(run["cancel_requested"]), "summary_path": run["summary_path"],
        "jobs": [_job_summary(store, job, detail=repository_id is not None) for job in jobs],
    }


def _stage_durations(telemetry: dict[str, Any]) -> dict[str, float]:
    durations: dict[str, float] = {}
    for span in telemetry.get("stages", []):
        durations[span["stage"]] = round(durations.get(span["stage"], 0.0) + span["duration_seconds"], 3)
    return durations


def _environment(telemetry: dict[str, Any]) -> dict[str, Any]:
    return {**{f"toolchain.{k}": v for k, v in (telemetry.get("toolchain") or {}).items()},
            **{f"execution.{k}": v for k, v in (telemetry.get("execution") or {}).items()},
            **{f"dependency.{k}": v for k, v in (telemetry.get("dependency") or {}).items()},
            "repository_commit": telemetry.get("repository_commit"),
            "llm.model": (telemetry.get("llm") or {}).get("model")}


def compare_runs(store: Store, left_id: str, right_id: str) -> dict[str, Any]:
    """Per-repository differences between two fleet runs: outcome, patch, timing, environment."""
    left_run, right_run = _fleet_run(store, left_id), _fleet_run(store, right_id)
    left = {j.repository_id: j for j in store.jobs(left_run["fleet_run_id"])}
    right = {j.repository_id: j for j in store.jobs(right_run["fleet_run_id"])}
    repositories = []
    for repository_id in sorted(set(left) | set(right)):
        a, b = left.get(repository_id), right.get(repository_id)
        ta, tb = (a.telemetry or {}) if a else {}, (b.telemetry or {}) if b else {}
        sa, sb = _stage_durations(ta), _stage_durations(tb)
        ea, eb = _environment(ta), _environment(tb)
        outcome_a = a.outcome.status.value if a and a.outcome else None
        outcome_b = b.outcome.status.value if b and b.outcome else None
        repositories.append({
            "repository_id": repository_id,
            "outcome": {"left": outcome_a, "right": outcome_b, "changed": outcome_a != outcome_b},
            "final_diff_identical": (ta.get("final_diff_sha256") == tb.get("final_diff_sha256")
                                     if ta.get("final_diff_sha256") and tb.get("final_diff_sha256") else None),
            "duration_seconds": {"left": ta.get("duration_seconds"), "right": tb.get("duration_seconds")},
            "stage_duration_delta": {
                stage: round(sb.get(stage, 0.0) - sa.get(stage, 0.0), 3) for stage in sorted(set(sa) | set(sb))
            },
            "repair_attempts": {"left": ta.get("repair_attempts"), "right": tb.get("repair_attempts")},
            "environment_changes": {
                key: {"left": ea.get(key), "right": eb.get(key)}
                for key in sorted(set(ea) | set(eb)) if ea.get(key) != eb.get(key) and (ta and tb)
            },
        })
    return {
        "left": left_run["fleet_run_id"], "right": right_run["fleet_run_id"],
        "outcome_changes": sum(r["outcome"]["changed"] for r in repositories),
        "repositories": repositories,
    }
