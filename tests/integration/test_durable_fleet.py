"""Durable fleet orchestration: persistence, crash recovery, stage-level resumption,
cancellation, duplicate prevention and bounded concurrent execution.

Stages are stand-ins (as in the engine unit tests); the store, leases, worker pool,
working copies and recovery logic are real.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from depbot.execution import ExecutionConfig, ResourceLimits
from depbot.fleet.registry import load_event, load_fleet
from depbot.fleet.runner import DuplicateEvent, FleetRunError, resume_fleet, run_fleet
from depbot.interfaces import StageError
from depbot.models import OutcomeStatus
from depbot.persistence.store import Store
from depbot.verification.command_runner import run_command
from tests.unit.test_orchestrator import MAIN, _passing, _stages

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL = ExecutionConfig(limits=ResourceLimits(cpus=0.1, memory_mb=64))  # never the scheduling bottleneck


class Crash(BaseException):
    """Stands in for a worker dying: not an Exception, so nothing in the engine handles it."""


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.time() + self.offset


def _make_repo(root, name, version="1.0.0"):
    repo = root / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.cpp").write_text(MAIN)
    (repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\nproject(x CXX)\n")
    (repo / "conanfile.txt").write_text(f"[requires]\ntoydep/{version}\n")


@pytest.fixture
def fleet_dir(tmp_path):
    names = ["repo_a", "repo_b", "repo_c"]
    for name in names:
        _make_repo(tmp_path, name)
    repos = "\n".join(f"  - {{repository_id: {n}, repo_url_or_path: {n}, display_name: {n}}}" for n in names)
    (tmp_path / "fleet.yaml").write_text(textwrap.dedent("""\
        fleet_id: test-fleet
        default_policy_id: demo
        policies:
          demo: {allowed_change_levels: [patch, minor, major], major_approved: true}
        repositories:
        """) + repos + "\n")
    (tmp_path / "event.yaml").write_text(textwrap.dedent("""\
        event_id: toydep-v2
        dependency_name: toydep
        upstream_repo: local/toydep
        previous_version: 1.0.0
        target_version: 2.0.0
        """))
    return tmp_path


def _args(fleet_dir, store=None):
    return dict(state_dir=fleet_dir / ".depbot", store=store or Store.for_state_dir(fleet_dir / ".depbot"))


def _run(fleet_dir, stages, store=None, **kw):
    kw.setdefault("execution", SMALL)
    return run_fleet(load_event(fleet_dir / "event.yaml"), load_fleet(fleet_dir / "fleet.yaml"), stages=stages,
                     artifacts_root=fleet_dir / "artifacts", **_args(fleet_dir, store), **kw)


def _status(summary):
    return {o.repository_id: o.status for o in summary.outcomes}


def _only_run(store):
    (run,) = store.fleet_runs()
    return run


# ------------------------------------------------------------------ persistence


def test_run_persists_event_jobs_transitions_stages_artifacts_and_telemetry(fleet_dir):
    store = Store.for_state_dir(fleet_dir / ".depbot")
    summary = _run(fleet_dir, _stages(), store)
    assert set(_status(summary).values()) == {OutcomeStatus.VERIFIED}
    run = _only_run(store)
    assert run["status"] == "completed" and run["summary_path"] == summary.summary_path
    assert [e.event_id for e in store.events()] == ["toydep-v2"]
    job = store.jobs(run["fleet_run_id"])[0]
    assert job.status == "completed" and job.stage == "VERIFIED" and job.attempt == 1 and job.worker_id
    assert job.outcome.status == OutcomeStatus.VERIFIED and job.state.status == "VERIFIED"
    path = [t["to_status"] for t in store.transitions(job.job_key)]
    assert path == ["INSPECTED", "RESEARCHED", "IMPACT_ANALYZED", "PLANNED", "PATCHED", "VERIFIED"]
    stages = [(s["stage"], s["status"]) for s in store.stage_runs(job.job_key)]
    assert stages == [(n, "ok") for n in
                      ("inspect", "probe", "research", "impact", "plan", "policy_gate", "patch", "verify")]
    kinds = {a["kind"] for a in store.artifacts(job.job_key)}
    assert {"state", "telemetry", "patch", "evidence", "report"} <= kinds
    assert all(Path(a["path"]).is_file() and len(a["sha256"]) == 64 for a in store.artifacts(job.job_key))
    telemetry = job.telemetry
    assert telemetry["run_id"] == job.run_id and telemetry["fleet_run_id"] == run["fleet_run_id"]
    assert telemetry["outcome"] == "verified" and telemetry["execution"]["backend"] == "local"
    assert telemetry["dependency"] == {"name": "toydep", "from_version": "1.0.0", "to_version": "2.0.0",
                                       "package_manager": "conan"}
    assert json.loads((Path(job.artifacts_dir) / "telemetry.json").read_text()) == telemetry


def test_duplicate_event_is_rejected_and_recorded_once(fleet_dir):
    store = Store.for_state_dir(fleet_dir / ".depbot")
    _run(fleet_dir, _stages(), store)
    with pytest.raises(DuplicateEvent, match="--force"):
        _run(fleet_dir, _stages(), store)
    assert len(store.events()) == 1 and len(store.fleet_runs()) == 1


# -------------------------------------------------------------- crash recovery


def _crashing(stage, calls, *, only="repo_a", when=lambda: True, **extra):
    """Stages where ``stage`` kills the worker for one repository (first time only)."""
    base = _stages()
    crashed = []

    def wrapper(**kw):
        calls.append(stage)
        worktree = kw.get("worktree")
        if not crashed and when() and (worktree is None or only in str(worktree)):
            crashed.append(1)
            raise Crash(f"worker died in {stage}")
        return getattr(base, stage)(**kw)

    def research(**kw):
        calls.append("research")
        return base.research(**kw)

    overrides = {"research": research, stage: wrapper, **extra}
    return _stages(**overrides)


def test_interrupted_job_resumes_from_its_last_stage_checkpoint(fleet_dir):
    clock, calls = Clock(), []
    store = Store(fleet_dir / ".depbot" / "depbot.db", clock=clock)
    stages = _crashing("impact", calls)
    summary = _run(fleet_dir, stages, store)
    run_id = _only_run(store)["fleet_run_id"]
    crashed = store.job(f"{run_id}:repo_a")
    assert crashed.status == "running" and crashed.stage == "RESEARCHED" and crashed.outcome is None
    assert _status(summary)["repo_a"] == OutcomeStatus.FAILED
    assert "depbot runs resume" in summary.outcomes[0].reason
    assert _status(summary)["repo_b"] == OutcomeStatus.VERIFIED  # the fleet kept going
    assert _only_run(store)["status"] == "running"  # not finished: a job is still leased
    with pytest.raises(DuplicateEvent, match="runs resume"):
        _run(fleet_dir, _stages(), store)

    # While the dead worker's lease is still valid nothing is taken over.
    still = resume_fleet(run_id, stages=stages, **_args(fleet_dir, store))
    assert _status(still)["repo_a"] == OutcomeStatus.FAILED and store.job(crashed.job_key).attempt == 1

    clock.offset = 10_000  # the lease expires
    research_calls = calls.count("research")
    resumed = resume_fleet(run_id, stages=stages, **_args(fleet_dir, store))
    assert set(_status(resumed).values()) == {OutcomeStatus.VERIFIED}
    job = store.job(crashed.job_key)
    assert job.status == "completed" and job.attempt == 2 and job.run_id == crashed.run_id
    assert calls.count("research") == research_calls, "a completed stage was re-run after recovery"
    assert [r["action"] for r in store.recovery_log(job.job_key)] == ["requeued", "resumed"]
    assert job.telemetry["resumed_from"] == "RESEARCHED" and job.telemetry["attempt"] == 2
    second = [s["stage"] for s in store.stage_runs(job.job_key) if s["attempt"] == 2]
    assert second == ["impact", "plan", "policy_gate", "patch", "verify"]
    assert _only_run(store)["status"] == "completed"
    assert "old_sum" not in (Path(job.state.worktree_path) / "src" / "main.cpp").read_text()


def test_policy_gate_is_re_evaluated_when_resuming_after_planning(fleet_dir, monkeypatch):
    """Dying between the PLANNED checkpoint and the gate must not let a plan skip the gate."""
    import depbot.orchestrator as orch

    clock, real_gate, gate_calls = Clock(), orch.plan_confidence_gate, []
    store = Store(fleet_dir / ".depbot" / "depbot.db", clock=clock)

    def gate(*args, **kw):
        gate_calls.append(1)
        if len(gate_calls) == 1:
            raise Crash("died before the policy gate decided")
        return real_gate(*args, **kw)

    monkeypatch.setattr(orch, "plan_confidence_gate", gate)
    _run(fleet_dir, _stages(), store)
    run_id = _only_run(store)["fleet_run_id"]
    assert store.job(f"{run_id}:repo_a").stage == "PLANNED"
    clock.offset = 10_000
    resumed = resume_fleet(run_id, stages=_stages(), **_args(fleet_dir, store))
    assert _status(resumed)["repo_a"] == OutcomeStatus.VERIFIED
    second = [s["stage"] for s in store.stage_runs(f"{run_id}:repo_a") if s["attempt"] == 2]
    assert second == ["policy_gate", "patch", "verify"]
    assert store.job(f"{run_id}:repo_a").state.policy_decisions  # the decision is on record


def test_interruption_after_the_manifest_edit_restarts_instead_of_resuming(fleet_dir):
    clock, calls = Clock(), []
    store = Store(fleet_dir / ".depbot" / "depbot.db", clock=clock)
    stages = _crashing("patch", calls)  # the manifest is already bumped when the patch stage runs
    _run(fleet_dir, stages, store)
    run_id = _only_run(store)["fleet_run_id"]
    clock.offset = 10_000
    resumed = resume_fleet(run_id, stages=stages, **_args(fleet_dir, store))
    assert _status(resumed)["repo_a"] == OutcomeStatus.VERIFIED
    assert [r["action"] for r in store.recovery_log(f"{run_id}:repo_a")] == ["requeued", "restarted"]


def test_half_applied_patch_is_never_resumed_the_job_restarts_clean(fleet_dir):
    """The worker dies after editing the working copy but before the PATCHED checkpoint."""
    clock, calls = Clock(), []
    store = Store(fleet_dir / ".depbot" / "depbot.db", clock=clock)
    base, crashed = _stages(), []

    def apply(**kw):
        result = base.apply(**kw)
        if not crashed and "repo_a" in str(kw["worktree"]):
            crashed.append(1)
            raise Crash("died after git apply")
        return result

    stages = _stages(apply=apply)
    _run(fleet_dir, stages, store)
    run_id = _only_run(store)["fleet_run_id"]
    first = store.job(f"{run_id}:repo_a")
    assert first.stage == "PLANNED" and first.status == "running"
    clock.offset = 10_000
    resumed = resume_fleet(run_id, stages=stages, **_args(fleet_dir, store))
    job = store.job(first.job_key)
    assert _status(resumed)["repo_a"] == OutcomeStatus.VERIFIED
    assert job.run_id != first.run_id, "restart must use a fresh run directory"
    log = store.recovery_log(job.job_key)
    assert [r["action"] for r in log] == ["requeued", "restarted"] and "uncheckpointed edits" in log[1]["detail"]
    assert job.telemetry["resumed_from"] is None and job.telemetry["attempt"] == 2
    assert Path(first.artifacts_dir).is_dir()  # evidence of the failed attempt is kept


def test_verified_job_interrupted_before_reporting_is_finalised_not_re_run(fleet_dir):
    clock, calls = Clock(), []
    store = Store(fleet_dir / ".depbot" / "depbot.db", clock=clock)
    base, crashed = _stages(), []

    def report(**kw):
        if not crashed and kw["state"].repository_id == "repo_a":
            crashed.append(1)
            raise Crash("died while reporting")
        return base.report(**kw)

    verifies = []

    def verify(phase, **kw):
        verifies.append(phase)
        return _passing(phase)

    stages = _stages(report=report, verify=verify)
    _run(fleet_dir, stages, store)
    run_id = _only_run(store)["fleet_run_id"]
    assert store.job(f"{run_id}:repo_a").stage == "VERIFIED"
    clock.offset, before = 10_000, len(verifies)
    resumed = resume_fleet(run_id, stages=stages, **_args(fleet_dir, store))
    job = store.job(f"{run_id}:repo_a")
    assert _status(resumed)["repo_a"] == OutcomeStatus.VERIFIED and job.outcome.verification_passed
    assert len(verifies) == before and job.outcome.report_path
    commits = subprocess.run(["git", "log", "--oneline"], cwd=job.state.worktree_path, capture_output=True,
                             text=True).stdout.splitlines()
    assert sum("depbot: upgrade" in line for line in commits) == 1


def test_job_that_keeps_killing_its_worker_is_abandoned(fleet_dir):
    clock = Clock()
    store = Store(fleet_dir / ".depbot" / "depbot.db", clock=clock)

    def impact(**kw):
        if "repo_a" in str(kw["worktree"]):
            raise Crash("always dies")
        return _stages().impact(**kw)

    stages = _stages(impact=impact)
    _run(fleet_dir, stages, store)
    run_id = _only_run(store)["fleet_run_id"]
    for _ in range(3):
        clock.offset += 10_000
        summary = resume_fleet(run_id, stages=stages, **_args(fleet_dir, store))
    job = store.job(f"{run_id}:repo_a")
    assert job.status == "abandoned" and job.attempt == 3
    assert _status(summary)["repo_a"] == OutcomeStatus.FAILED and "abandoned" in summary.outcomes[0].reason
    assert _only_run(store)["status"] == "completed"


CRASH_SCRIPT = """
import os, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from depbot.fleet.registry import load_event, load_fleet
from depbot.fleet.runner import run_fleet
from tests.unit.test_orchestrator import _stages

def impact(**kw):
    if "repo_b" in str(kw["worktree"]):
        os._exit(137)  # the whole process dies, like a kill -9 or OOM
    return _stages().impact(**kw)

root = Path({root!r})
run_fleet(load_event(root / "event.yaml"), load_fleet(root / "fleet.yaml"), stages=_stages(impact=impact),
          state_dir=root / ".depbot", artifacts_root=root / "artifacts", lease_seconds=1)
"""


def test_recovery_after_the_orchestrator_process_is_killed(fleet_dir, monkeypatch):
    """A real process death: no finally blocks, no summary, only what SQLite already holds."""
    script = fleet_dir / "crash.py"
    script.write_text(CRASH_SCRIPT.format(repo=str(REPO_ROOT), root=str(fleet_dir)))
    env = {**os.environ, "PYTHONPATH": f"{REPO_ROOT / 'src'}{os.pathsep}{REPO_ROOT}"}
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137, proc.stderr
    store = Store.for_state_dir(fleet_dir / ".depbot")
    run = _only_run(store)
    rows = {r.repository_id: r for r in store.jobs(run["fleet_run_id"])}
    assert run["status"] == "running" and run["summary_path"] is None
    assert rows["repo_a"].status == "completed" and rows["repo_a"].outcome.status == OutcomeStatus.VERIFIED
    assert rows["repo_b"].status == "running" and rows["repo_b"].stage == "RESEARCHED"
    assert rows["repo_c"].status == "queued"

    time.sleep(1.2)  # the dead process's 1-second lease runs out
    summary = resume_fleet(run["fleet_run_id"], stages=_stages(), **_args(fleet_dir, store))
    assert set(_status(summary).values()) == {OutcomeStatus.VERIFIED}
    rows = {r.repository_id: r for r in store.jobs(run["fleet_run_id"])}
    assert rows["repo_a"].attempt == 1 and rows["repo_b"].attempt == 2 and rows["repo_c"].attempt == 1
    assert rows["repo_b"].telemetry["resumed_from"] == "RESEARCHED"
    assert _only_run(store)["status"] == "completed" and Path(summary.summary_path).is_file()


def test_forced_rerun_does_not_duplicate_a_job_that_is_still_active(fleet_dir):
    store = Store.for_state_dir(fleet_dir / ".depbot")
    _run(fleet_dir, _crashing("impact", []), store)
    first = _only_run(store)["fleet_run_id"]
    summary = _run(fleet_dir, _stages(), store, force=True)
    blocked = next(o for o in summary.outcomes if o.repository_id == "repo_a")
    assert blocked.status == OutcomeStatus.BLOCKED and f"{first}:repo_a" in blocked.reason
    assert _status(summary)["repo_b"] == OutcomeStatus.VERIFIED
    active = [r for run in store.fleet_runs() for r in store.jobs(run["fleet_run_id"])
              if r.repository_id == "repo_a" and r.status in ("queued", "running")]
    assert len(active) == 1


def test_resume_of_unknown_or_cancelled_run_is_refused(fleet_dir):
    store = Store.for_state_dir(fleet_dir / ".depbot")
    with pytest.raises(FleetRunError, match="unknown fleet run"):
        resume_fleet("nope", stages=_stages(), **_args(fleet_dir, store))


# ---------------------------------------------------------------- cancellation


def test_cancellation_stops_the_running_job_and_never_starts_queued_ones(fleet_dir):
    store = Store.for_state_dir(fleet_dir / ".depbot")
    cancel = threading.Event()
    started = []

    def impact(**kw):
        started.append(Path(kw["worktree"]).parent.name)
        cancel.set()  # operator presses Ctrl-C while the first job is analysing impact
        return _stages().impact(**kw)

    summary = _run(fleet_dir, _stages(impact=impact), store, cancel=cancel)
    assert len(started) == 1
    assert set(_status(summary).values()) == {OutcomeStatus.BLOCKED}
    assert all("cancelled" in o.reason for o in summary.outcomes)
    run = _only_run(store)
    statuses = [r.status for r in store.jobs(run["fleet_run_id"])]
    assert statuses == ["cancelled", "cancelled", "cancelled"] and run["status"] == "cancelled"
    first = store.jobs(run["fleet_run_id"])[0]
    assert first.stage == "BLOCKED" and first.attempt == 1 and store.jobs(run["fleet_run_id"])[1].attempt == 0
    assert [s["status"] for s in store.stage_runs(first.job_key)][-1] == "cancelled"
    with pytest.raises(FleetRunError, match="was cancelled"):
        resume_fleet(run["fleet_run_id"], stages=_stages(), **_args(fleet_dir, store))


def test_durable_cancel_request_kills_a_running_command(fleet_dir):
    """`depbot runs cancel` from another process: the flag in SQLite stops the build."""
    store = Store.for_state_dir(fleet_dir / ".depbot")

    def verify(*, worktree, phase, artifacts_dir, **_):
        if phase != "upgraded":
            return _passing(phase)
        step = run_command(command_label=f"{phase}.build", cwd=worktree, artifacts_dir=artifacts_dir,
                           args=[sys.executable, "-c", "import time; time.sleep(120)"], timeout_seconds=300)
        return _passing(phase).model_copy(update={"build": step, "passed": False})

    def cancel_soon():
        deadline = time.time() + 30
        while time.time() < deadline:
            runs = store.fleet_runs()
            if runs and any(r.stage == "PATCHED" for r in store.jobs(runs[0]["fleet_run_id"])):
                time.sleep(0.3)
                store.request_cancel(runs[0]["fleet_run_id"])
                return
            time.sleep(0.05)

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    summary = _run(fleet_dir, _stages(verify=verify), store)
    assert time.monotonic() - started < 60, "the sleeping build was not killed"
    assert set(_status(summary).values()) == {OutcomeStatus.BLOCKED}
    first = store.jobs(_only_run(store)["fleet_run_id"])[0]
    assert first.status == "cancelled" and "cancel" in first.outcome.reason
    assert first.state.status == "BLOCKED"  # not misreported as a build failure


# ------------------------------------------------------- concurrent execution


def test_jobs_run_concurrently_up_to_the_worker_limit(fleet_dir):
    active, peak, lock = [0], [0], threading.Lock()

    def research(**kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.4)
        with lock:
            active[0] -= 1
        return _stages().research(**kw)

    store = Store.for_state_dir(fleet_dir / ".depbot")
    summary = _run(fleet_dir, _stages(research=research), store, workers=2)
    assert set(_status(summary).values()) == {OutcomeStatus.VERIFIED}
    assert peak[0] == 2
    assert [o.repository_id for o in summary.outcomes] == ["repo_a", "repo_b", "repo_c"]  # stable order
    workers = {r.worker_id for r in store.jobs(_only_run(store)["fleet_run_id"])}
    assert len(workers) == 2
    run_dirs = {r.artifacts_dir for r in store.jobs(_only_run(store)["fleet_run_id"])}
    assert len(run_dirs) == 3  # per-job state and working copy


def test_one_failing_job_does_not_affect_concurrent_jobs(fleet_dir):
    def plan(**kw):
        raise RuntimeError("bug in planner")

    def impact(**kw):
        if "repo_b" in str(kw["worktree"]):
            raise StageError("cannot index repo_b", blocked=True)
        return _stages().impact(**kw)

    base = _stages(impact=impact)

    def flaky_plan(**kw):
        if threading.current_thread().name and kw["usages"] and getattr(flaky_plan, "fail_next", True):
            flaky_plan.fail_next = False
            return plan(**kw)
        return base.plan(**kw)

    summary = _run(fleet_dir, _stages(impact=impact, plan=flaky_plan), workers=3)
    statuses = sorted(s.value for s in _status(summary).values())
    assert statuses == ["blocked", "failed", "verified"]
    assert summary.repositories_verified == 1 and summary.repositories_failed_or_blocked == 2


def test_worker_count_is_limited_by_the_resource_budget(fleet_dir):
    store = Store.for_state_dir(fleet_dir / ".depbot")
    huge = ExecutionConfig(limits=ResourceLimits(cpus=64, memory_mb=1_000_000_000))
    _run(fleet_dir, _stages(), store, workers=8, execution=huge)
    assert json.loads(_only_run(store)["config"])["workers"] == 1
