"""DepBot command-line interface (owner: Member A).

    depbot fleet status --fleet examples/demo-fleet.yaml
    depbot fleet plan   --fleet examples/demo-fleet.yaml --event examples/events/toydep-v2.yaml
    depbot run          --fleet examples/demo-fleet.yaml --event examples/events/toydep-v2.yaml --approve-major
    depbot upgrade      --repo PATH --dependency toydep --to 2.0.0 --approve-major
    depbot runs list|show|compare|resume|cancel --fleet examples/demo-fleet.yaml [...]
    depbot bench run    --registry benchmarks/registry.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from depbot.benchmark.models import BenchmarkError, BenchmarkResults, load_registry
from depbot.benchmark.runner import BenchmarkRunner, default_out_dir, tree_sha256, write_results
from depbot.fleet.dependency_index import DependencyIndex, scan_repository
from depbot.fleet.events import EventLog
from depbot.fleet.monitor import PollReport, poll_fleet_once, run_monitor
from depbot.fleet.registry import Fleet, RegistryError, load_event, load_fleet
from pydantic import ValidationError

from depbot.execution import (
    ExecutionConfig,
    ExecutionConfigError,
    ExecutionContext,
    ResourceLimits,
    build_backend,
)
from depbot.fleet.runner import DuplicateEvent, FleetRunError, resume_fleet, run_fleet
from depbot.fleet.selection import RepositorySelection, select_repositories, state_dir_for
from depbot.fleet.versions import UnparseableVersion, compare
from depbot.models import (
    ChangeLevel,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryRecord,
    RepositoryUpgradeJob,
    UpgradePolicy,
)
from depbot.llm.base import LLMError
from depbot.llm.gemini import GeminiLLMClient
from depbot.llm.provider import AnthropicLLMClient
from depbot.llm.runtime import LLMFactory, fixture_factory
from depbot.orchestrator import load_default_stages, run_repository_job
from depbot.package_managers.conan import CONAN_HOME_ENV
from depbot.package_managers.conan_setup import SetupError, prepare_local_packages
from depbot.persistence.store import DB_NAME, Store, StoreError
from depbot.policy.engine import pre_migration_gate
from depbot.reporting.run_inspect import UnknownRun, compare_runs, list_runs, show_run
from depbot.repository.remote import RemoteRepositoryError, is_remote, sync_remote

_GOOD = {OutcomeStatus.VERIFIED, OutcomeStatus.UNAFFECTED, OutcomeStatus.SKIPPED}

log = logging.getLogger("depbot")


def _use_conan_home(path: Path) -> None:
    """Point every DepBot Conan command at an isolated home (explicit env wins)."""
    if path.is_dir() and CONAN_HOME_ENV not in os.environ:
        os.environ[CONAN_HOME_ENV] = str(path.resolve())


def _load(args: argparse.Namespace) -> tuple[Fleet, Path]:
    fleet = load_fleet(Path(args.fleet))
    if getattr(args, "approve_major", False):
        for pid, policy in fleet.config.policies.items():
            fleet.config.policies[pid] = policy.model_copy(update={"major_approved": True})
    state_dir = state_dir_for(fleet, Path(args.state_dir) if args.state_dir else None)
    _use_conan_home(state_dir / "conan")
    return fleet, state_dir


def cmd_demo_setup(args: argparse.Namespace) -> int:
    """Build local dependency recipes (e.g. toydep 1.0.0 and 2.0.0) into the fleet's
    isolated Conan home so `depbot run` works offline without touching ~/.conan2."""
    fleet = load_fleet(Path(args.fleet))
    state_dir = state_dir_for(fleet, Path(args.state_dir) if args.state_dir else None)
    conan_home = state_dir / "conan"
    try:
        steps = prepare_local_packages([Path(r) for r in args.recipe], conan_home)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for step in steps:
        verdict = "ok" if step.succeeded else ("TIMEOUT" if step.timed_out else f"FAILED ({step.exit_code})")
        print(f"  {step.label:<28} {verdict:<12} {step.duration_seconds:.1f}s")
        if not step.succeeded:
            print(step.output_tail, file=sys.stderr)
    ok = all(s.succeeded for s in steps) and len(steps) == len(args.recipe) + 2
    print(f"conan home: {conan_home.resolve()}" if ok else "demo setup failed")
    return 0 if ok else 1


def cmd_fleet_status(args: argparse.Namespace) -> int:
    fleet, state_dir = _load(args)
    index = DependencyIndex(state_dir / "inventory.json")
    rows = []
    for repo in fleet.repositories:
        try:
            root, _ = fleet.resolve_root(repo, state_dir / "sources")
            scan, rescanned = index.refresh(repo, root) if root.is_dir() else (None, False)
        except RegistryError as exc:
            rows.append({"repository_id": repo.repository_id, "error": str(exc)})
            continue
        rows.append(
            {
                "repository_id": repo.repository_id,
                "enabled": repo.enabled,
                "path": str(root),
                "commit": (scan.indexed_commit or "-")[:10] if scan else "-",
                "rescanned": rescanned,
                "dependencies": [
                    f"{d.dependency_name}/{d.current_version} ({d.package_manager.value})"
                    for d in (scan.dependencies if scan else [])
                ],
                "error": (scan.error if scan else f"path not found: {root}"),
            }
        )
    index.save()
    if args.json:
        print(json.dumps({"fleet_id": fleet.fleet_id, "repositories": rows}, indent=2))
        return 0
    print(f"Fleet {fleet.fleet_id}: {len(rows)} registered repositories")
    for row in rows:
        deps = ", ".join(row.get("dependencies") or []) or "-"
        status = "enabled" if row.get("enabled", True) else "disabled"
        line = f"  {row['repository_id']:<16} {status:<9} deps: {deps}"
        if row.get("error"):
            line += f"  [error: {row['error']}]"
        print(line)
    return 0


def _selection_row(sel: RepositorySelection) -> dict:
    return {
        "repository_id": sel.repository.repository_id,
        "classification": sel.classification.value,
        "eligible_for_job": sel.eligible,
        "reason": sel.reason,
        "current_version": sel.dependency_record.current_version if sel.dependency_record else None,
        "policy": sel.policy_decision.model_dump(mode="json") if sel.policy_decision else None,
        "rescanned": sel.rescanned,
    }


def cmd_fleet_plan(args: argparse.Namespace) -> int:
    """Dry run: classify the fleet for an event without recording or running anything."""
    fleet, state_dir = _load(args)
    event = load_event(Path(args.event))
    duplicate = EventLog(state_dir / "events.json").find_duplicate(event)
    selections = select_repositories(
        event, fleet, DependencyIndex(state_dir / "inventory.json"), sources_dir=state_dir / "sources"
    )
    rows = [_selection_row(s) for s in selections]
    jobs = [r["repository_id"] for r in rows if r["eligible_for_job"]]
    if args.json:
        print(
            json.dumps(
                {
                    "event_id": event.event_id,
                    "already_processed": duplicate is not None,
                    "jobs": jobs,
                    "repositories": rows,
                },
                indent=2,
            )
        )
        return 0
    print(
        f"Event {event.event_id}: {event.dependency_name} "
        f"{event.previous_version or '?'} -> {event.target_version}"
    )
    if duplicate is not None:
        print(f"  note: already processed as {duplicate.event_id}")
    for sel in selections:
        verdict = "JOB" if sel.eligible else sel.to_outcome(event).status.value.upper()
        detail = sel.reason
        if sel.policy_decision and not sel.policy_decision.allowed:
            detail += " | policy: " + "; ".join(sel.policy_decision.reasons)
        print(f"  {verdict:<8} {sel.repository.repository_id:<16} {sel.classification.value:<27} {detail}")
    print(f"{len(jobs)} job(s) would be scheduled: {', '.join(jobs) or 'none'}")
    return 0


LLM_MODES = ("none", "fixture", "anthropic", "gemini")


def _llm_factory(args: argparse.Namespace) -> LLMFactory | None:
    """Build the per-job LLM client factory from --llm (or $DEPBOT_LLM). Validated up front."""
    mode = args.llm or os.environ.get("DEPBOT_LLM") or "none"
    if mode not in LLM_MODES:
        raise LLMError(f"unknown LLM mode {mode!r}; choose one of {', '.join(LLM_MODES)}")
    if mode == "none":
        return None
    if mode == "fixture":
        if not args.llm_fixtures:
            raise LLMError("--llm fixture requires --llm-fixtures PATH")
        return fixture_factory(Path(args.llm_fixtures))
    if mode == "gemini":
        GeminiLLMClient.from_env(model=args.llm_model)  # fail fast if no key
        return lambda: GeminiLLMClient.from_env(model=args.llm_model)
    AnthropicLLMClient.from_env(model=args.llm_model)  # fail fast if no key
    return lambda: AnthropicLLMClient.from_env(model=args.llm_model)


def _add_llm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--llm", choices=LLM_MODES,
                        help="LLM provider for bounded stages (default: $DEPBOT_LLM or none)")
    parser.add_argument("--llm-fixtures", help="JSON {task: response} file for --llm fixture")
    parser.add_argument("--llm-model", help="model for --llm anthropic or gemini (default: $DEPBOT_LLM_MODEL)")


def _add_exec_args(parser: argparse.ArgumentParser, *, workers: bool = True) -> None:
    group = parser.add_argument_group("execution")
    if workers:
        group.add_argument("--workers", type=int, default=1,
                           help="max concurrent repository jobs (default 1; capped by host CPU/memory)")
    group.add_argument("--exec-backend", choices=("local", "docker", "podman"), default=None,
                       help="where install/configure/build/test run (default: $DEPBOT_EXEC_BACKEND or local)")
    group.add_argument("--exec-image", help="container image with the build toolchain (pin by digest)")
    group.add_argument("--exec-network", choices=("none", "bridge"), default="none",
                       help="container network (default: none)")
    group.add_argument("--exec-network-install", action="store_true",
                       help="allow network only for the dependency-install step")
    group.add_argument("--exec-cpus", type=float, help="CPU limit per command (default 2)")
    group.add_argument("--exec-memory-mb", type=int, help="memory limit per command in MiB (default 4096)")
    group.add_argument("--exec-pids", type=int, help="process limit per command (default 512)")
    group.add_argument("--exec-mount-ro", action="append", default=[], metavar="PATH",
                       help="extra host path mounted read-only in the container (repeatable)")
    group.add_argument("--exec-mount-rw", action="append", default=[], metavar="PATH",
                       help="extra host path mounted read-write in the container (repeatable)")


def _execution_config(args: argparse.Namespace) -> ExecutionConfig:
    """Execution settings from --exec-* flags. Raises RegistryError-compatible ValueError when unsafe."""
    backend = getattr(args, "exec_backend", None) or os.environ.get("DEPBOT_EXEC_BACKEND") or "local"
    limits = {k: v for k, v in (("cpus", getattr(args, "exec_cpus", None)),
                                ("memory_mb", getattr(args, "exec_memory_mb", None)),
                                ("pids", getattr(args, "exec_pids", None))) if v is not None}
    read_write = [str(Path(p).resolve()) for p in getattr(args, "exec_mount_rw", [])]
    conan_home = os.environ.get(CONAN_HOME_ENV)
    if backend != "local" and conan_home and Path(conan_home).is_dir():
        read_write.append(str(Path(conan_home).resolve()))  # the fleet's isolated package cache
    return ExecutionConfig(
        backend=backend,
        image=getattr(args, "exec_image", None) or os.environ.get("DEPBOT_EXEC_IMAGE"),
        limits=ResourceLimits(**limits),
        network=getattr(args, "exec_network", "none"),
        network_for_install=getattr(args, "exec_network_install", False),
        read_only_paths=[str(Path(p).resolve()) for p in getattr(args, "exec_mount_ro", [])],
        read_write_paths=sorted(set(read_write)),
    )


def _cancel_on_signal() -> threading.Event:
    """First Ctrl-C/SIGTERM cancels cooperatively (commands are killed, state is saved)."""
    cancel = threading.Event()

    def handler(*_):
        if cancel.is_set():
            raise KeyboardInterrupt
        print("cancelling: stopping running jobs (press Ctrl-C again to abort hard)", file=sys.stderr)
        cancel.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except ValueError:  # not the main thread (tests, embedding)
            pass
    return cancel


def _print_summary(summary) -> None:
    print(
        f"Event {summary.event_id}: {summary.dependency_name} "
        f"{summary.from_version or '?'} -> {summary.to_version}"
    )
    for o in summary.outcomes:
        print(f"  {o.status.value.upper():<10} {o.repository_id:<16} {o.reason or ''}")
    print(
        f"considered={summary.repositories_considered} affected={summary.repositories_affected} "
        f"verified={summary.repositories_verified} skipped={summary.repositories_skipped} "
        f"failed_or_blocked={summary.repositories_failed_or_blocked}"
    )
    print(f"fleet summary: {summary.summary_path}")


def cmd_run(args: argparse.Namespace) -> int:
    """Process one release event across the fleet. Exit 0 only if no job failed/blocked."""
    fleet, state_dir = _load(args)
    event = load_event(Path(args.event))
    llm_factory = _llm_factory(args)
    try:
        summary = run_fleet(
            event,
            fleet,
            stages=load_default_stages(),
            state_dir=state_dir,
            artifacts_root=Path(args.artifacts).resolve(),
            force=args.force,
            create_pr=args.create_pr,
            llm_factory=llm_factory,
            workers=args.workers,
            execution=_execution_config(args),
            cancel=_cancel_on_signal(),
        )
    except DuplicateEvent as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(summary.model_dump_json(indent=2))
    else:
        _print_summary(summary)
    return 0 if all(o.status in _GOOD for o in summary.outcomes) else 1


def _store(args: argparse.Namespace) -> tuple[Store, Path]:
    fleet, state_dir = _load(args)
    if not (state_dir / DB_NAME).is_file():
        raise RegistryError(f"no run history for this fleet yet ({state_dir / DB_NAME} does not exist)")
    return Store.for_state_dir(state_dir), state_dir


def cmd_runs_list(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    rows = list_runs(store)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        jobs = " ".join(f"{k}={v}" for k, v in sorted(row["jobs"].items())) or "-"
        outcomes = " ".join(f"{k}={v}" for k, v in sorted(row["outcomes"].items())) or "-"
        print(f"{row['fleet_run_id']}  {row['status']:<10} {row['created_at']}  jobs[{jobs}]  outcomes[{outcomes}]")
    if not rows:
        print("no fleet runs recorded")
    return 0


def cmd_runs_show(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    data = show_run(store, args.run, repository_id=args.job)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{data['fleet_run_id']}  event={data['event_id']}  status={data['status']}"
          f"{'  (cancel requested)' if data['cancel_requested'] else ''}")
    for job in data["jobs"]:
        print(f"  {job['repository_id']:<16} {job['status']:<10} stage={job['stage'] or '-':<18} "
              f"attempt={job['attempt']} outcome={job['outcome'] or '-'}  {job['reason'] or ''}")
        for stage in job["stages"]:
            print(f"      [{stage['attempt']}] {stage['stage']:<12} {stage['status']:<9} "
                  f"{stage['duration_seconds']:.2f}s  {stage['detail'] or ''}")
        for entry in job["recovery"]:
            print(f"      recovery: {entry['action']} ({entry['detail']})")
        if job.get("artifacts"):
            print(f"      artifacts: {len(job['artifacts'])} indexed under {job['artifacts_dir']}")
    return 0


def cmd_runs_compare(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    data = compare_runs(store, args.left, args.right)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{data['left']}  vs  {data['right']}: {data['outcome_changes']} outcome change(s)")
    for repo in data["repositories"]:
        o = repo["outcome"]
        marker = "CHANGED" if o["changed"] else "same"
        diff = {True: "identical patch", False: "DIFFERENT patch", None: "patch n/a"}[repo["final_diff_identical"]]
        d = repo["duration_seconds"]
        print(f"  {repo['repository_id']:<16} {marker:<8} {o['left'] or '-'} -> {o['right'] or '-'}  {diff}  "
              f"{d['left'] if d['left'] is not None else '-'}s -> {d['right'] if d['right'] is not None else '-'}s")
        for key, change in repo["environment_changes"].items():
            print(f"      {key}: {change['left']} -> {change['right']}")
    return 0


def cmd_runs_cancel(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = show_run(store, args.run)
    cancelled = store.request_cancel(run["fleet_run_id"])
    print(f"cancel requested for {run['fleet_run_id']}: {cancelled} queued job(s) cancelled; "
          "running jobs stop at their next check")
    return 0


def cmd_runs_resume(args: argparse.Namespace) -> int:
    store, state_dir = _store(args)
    run = show_run(store, args.run)
    summary = resume_fleet(
        run["fleet_run_id"], stages=load_default_stages(), state_dir=state_dir, store=store,
        llm_factory=_llm_factory(args), workers=args.workers,
        execution=_execution_config(args) if args.exec_backend else None, cancel=_cancel_on_signal(),
    )
    if args.json:
        print(summary.model_dump_json(indent=2))
    else:
        _print_summary(summary)
    return 0 if all(o.status in _GOOD for o in summary.outcomes) else 1


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Targeted single-repository upgrade through the same migration engine."""
    if args.conan_home:
        _use_conan_home(Path(args.conan_home))
    if is_remote(args.repo):
        name = args.repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "repo"
        repo = RepositoryRecord(repository_id=name, repo_url_or_path=args.repo, display_name=name,
                                default_branch=args.base)
        try:
            synced = sync_remote(repo, Path(args.state_dir or ".depbot").resolve() / "sources")
        except RemoteRepositoryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        root = Path(synced.path)
        print(f"synced {synced.url} ({synced.branch} @ {synced.commit[:10]}) -> {root}")
    else:
        root = Path(args.repo).resolve()
        repo = RepositoryRecord(repository_id=root.name, repo_url_or_path=str(root), display_name=root.name,
                                default_branch=args.base)
    scan = scan_repository(repo, root)
    if scan.error:
        print(f"error: {scan.error}", file=sys.stderr)
        return 2
    matches = [d for d in scan.dependencies if d.dependency_name == args.dependency]
    if len(matches) != 1:
        print(f"error: {args.dependency} declared {len(matches)} times in {root}", file=sys.stderr)
        return 2
    record = matches[0]
    levels = [ChangeLevel.PATCH, ChangeLevel.MINOR] + ([ChangeLevel.MAJOR] if args.approve_major else [])
    policy = UpgradePolicy(policy_id="cli", allowed_change_levels=levels, major_approved=args.approve_major)
    try:
        if compare(record.current_version, args.to) >= 0:
            print(f"nothing to do: {args.dependency} is already {record.current_version}")
            return 0
    except UnparseableVersion as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    decision = pre_migration_gate(policy, record.current_version, args.to)
    if not decision.allowed:
        print("blocked by policy: " + "; ".join(decision.reasons), file=sys.stderr)
        return 1
    event = ReleaseEvent(
        event_id=f"manual-{args.dependency}-{args.to}",
        dependency_name=args.dependency,
        upstream_repo=args.upstream_repo or "unknown",
        previous_version=record.current_version,
        target_version=args.to,
        changelog_path=str(Path(args.changelog).resolve()) if args.changelog else None,
    )
    job = RepositoryUpgradeJob(
        job_id=event.event_id, event=event, repository=repo, dependency_record=record,
        policy=policy, repo_root=str(root), create_pr=args.create_pr,
    )
    backend = build_backend(_execution_config(args))
    if backend.problems():
        print(f"error: execution backend '{backend.name}' is unusable: " + "; ".join(backend.problems()),
              file=sys.stderr)
        return 2
    state, outcome = run_repository_job(
        job, load_default_stages(), artifacts_root=Path(args.artifacts).resolve(),
        llm_factory=_llm_factory(args), execution=ExecutionContext(backend=backend),
    )
    if args.json:
        print(outcome.model_dump_json(indent=2))
    else:
        print(f"{outcome.status.value.upper()}: {outcome.reason or ''}")
        print(f"artifacts: {state.artifacts_dir}")
    return 0 if outcome.status in _GOOD else 1


def cmd_bench_list(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.registry))
    if args.json:
        print(registry.model_dump_json(indent=2))
        return 0
    print(f"{registry.suite} {registry.suite_version}: {len(registry.cases)} case(s)")
    for case in registry.cases:
        print(f"  {case.id:<36} {case.split:<9} {case.dependency} {case.baseline_version}->{case.target_version}"
              f"  expect={case.expected.outcome.value}  [{', '.join(case.tags)}]")
    return 0


def cmd_bench_hash(args: argparse.Namespace) -> int:
    for raw in args.path:
        print(f"{tree_sha256(Path(raw).resolve())}  {raw}")
    return 0


def cmd_bench_run(args: argparse.Namespace) -> int:
    """Run benchmark cases through the real engine. Exit 1 on any false-verified outcome or harness error."""
    registry_path = Path(args.registry)
    registry = load_registry(registry_path)
    unknown = sorted(set(args.case) - {c.id for c in registry.cases})
    if unknown:
        raise BenchmarkError(f"unknown case id: {', '.join(unknown)}")
    splits = ["dev", "held_out"] if args.split == "all" or args.case else [args.split]
    backend = build_backend(_execution_config(args))
    if backend.problems():
        raise FleetRunError(f"execution backend '{backend.name}' is unusable: " + "; ".join(backend.problems()))
    out_dir = Path(args.out).resolve() if args.out else default_out_dir(Path(args.artifacts).resolve())
    llm_mode = args.llm or os.environ.get("DEPBOT_LLM") or "none"
    runner = BenchmarkRunner(registry, registry_path, out_dir, stages_factory=load_default_stages,
                             llm_factory=_llm_factory(args), llm_label=llm_mode, backend=backend)

    def progress(result) -> None:
        if not args.json:
            verdict = result.status.upper() if result.status != "completed" else (
                "ok" if result.outcome_match and result.patch_correct is not False else "MISS")
            print(f"  {verdict:<8} {result.case_id:<36} expected={result.expected_outcome:<10} "
                  f"actual={result.actual_outcome or '-':<10} {result.reason or ''}", flush=True)

    results = runner.run(splits=splits, case_ids=args.case or None, on_case=progress)
    if args.json:
        print(results.model_dump_json(indent=2))
    else:
        overall = results.aggregate["overall"]
        print(f"completed={overall.completed} not_run={overall.not_run} errors={overall.errors} "
              f"outcome_accuracy={overall.outcome_accuracy} false_verified={overall.false_verified}")
        print(f"results: {out_dir / 'results.json'}\nreport:  {out_dir / 'report.md'}")
    overall = results.aggregate["overall"]
    return 1 if overall.false_verified or overall.errors else 0


def cmd_bench_report(args: argparse.Namespace) -> int:
    path = Path(args.results)
    results = BenchmarkResults.model_validate_json(path.read_text(encoding="utf-8"))
    _, report = write_results(results, path.parent)
    print(report.read_text(encoding="utf-8") if args.stdout else f"report: {report}")
    return 0


def _print_poll_report(report: PollReport, as_json: bool) -> None:
    if as_json:
        print(report.model_dump_json(indent=2))
        return
    print(f"polled {len(report.watches)} watch(es): {', '.join(report.watches) or 'none'}")
    for note in report.notes:
        print(f"  note: {note}")
    for failure in report.failures:
        print(f"  FAILED  {failure}")
    if not report.events and not report.failures and report.watches:
        print("  no new releases")
    for outcome in report.events:
        e = outcome.event
        head = f"  {outcome.action.upper():<9} {e.event_id} ({e.dependency_name} {e.previous_version} -> {e.target_version})"
        if outcome.summary is not None:
            s = outcome.summary
            head += (f": verified={s.repositories_verified} skipped={s.repositories_skipped} "
                     f"failed_or_blocked={s.repositories_failed_or_blocked} | {s.summary_path}")
        elif outcome.jobs or outcome.action == "dry_run":
            head += f": would run jobs for {', '.join(outcome.jobs) or 'no repositories'}"
        elif outcome.detail:
            head += f": {outcome.detail}"
        print(head)


def cmd_monitor(args: argparse.Namespace) -> int:
    """Poll watched upstreams; process new releases across the fleet."""
    fleet, state_dir = _load(args)
    if not fleet.config.watches:
        print("error: the fleet config has no `watches:`; nothing to monitor", file=sys.stderr)
        return 2
    llm_factory = _llm_factory(args)
    kwargs = dict(state_dir=state_dir, artifacts_root=Path(args.artifacts).resolve(),
                  stages_factory=load_default_stages, create_pr=args.create_pr,
                  llm_factory=llm_factory, dry_run=args.dry_run,
                  workers=args.workers, execution=_execution_config(args))
    if args.once:
        report = poll_fleet_once(fleet, **kwargs)
        _print_poll_report(report, args.json)
        return 0 if report.ok else 1
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"monitoring {len(fleet.config.watches)} watch(es) every {args.interval:.0f}s; Ctrl-C to stop")
    try:
        run_monitor(fleet, interval_seconds=args.interval, stop=stop,
                    on_report=lambda r: _print_poll_report(r, args.json), **kwargs)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("monitor stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="depbot", description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    fleet = sub.add_parser("fleet", help="fleet registry and selection").add_subparsers(
        dest="fleet_command", required=True
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fleet", required=True, help="fleet YAML config")
    common.add_argument("--state-dir", help="registry state dir (default: <fleet dir>/.depbot)")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    status = fleet.add_parser("status", parents=[common], help="show registered repos and inventory")
    status.set_defaults(func=cmd_fleet_status)

    plan = fleet.add_parser("plan", parents=[common], help="dry-run fleet selection for an event")
    plan.add_argument("--event", required=True, help="ReleaseEvent YAML/JSON file")
    plan.add_argument("--approve-major", action="store_true", help="operator approval for major upgrades")
    plan.set_defaults(func=cmd_fleet_plan)

    run = sub.add_parser("run", parents=[common], help="process a release event across the fleet")
    run.add_argument("--event", required=True, help="ReleaseEvent YAML/JSON file")
    run.add_argument("--approve-major", action="store_true", help="operator approval for major upgrades")
    run.add_argument("--force", action="store_true", help="reprocess an already-processed event")
    run.add_argument("--artifacts", default="artifacts", help="artifacts root (default: ./artifacts)")
    run.add_argument("--create-pr", action="store_true", help="open GitHub PRs for verified repos")
    _add_llm_args(run)
    _add_exec_args(run)
    run.set_defaults(func=cmd_run)

    runs = sub.add_parser("runs", help="inspect, compare, resume and cancel fleet runs").add_subparsers(
        dest="runs_command", required=True
    )
    runs.add_parser("list", parents=[common], help="recorded fleet runs").set_defaults(func=cmd_runs_list)
    show = runs.add_parser("show", parents=[common], help="jobs, stages and recovery history of one run")
    show.add_argument("run", help="fleet run id (or unique prefix)")
    show.add_argument("--job", metavar="REPOSITORY_ID", help="full telemetry/transitions/artifacts of one job")
    show.set_defaults(func=cmd_runs_show)
    cmp_ = runs.add_parser("compare", parents=[common], help="differences between two fleet runs")
    cmp_.add_argument("left")
    cmp_.add_argument("right")
    cmp_.set_defaults(func=cmd_runs_compare)
    cancel = runs.add_parser("cancel", parents=[common], help="stop a fleet run (also from another terminal)")
    cancel.add_argument("run")
    cancel.set_defaults(func=cmd_runs_cancel)
    resume = runs.add_parser("resume", parents=[common], help="continue an interrupted fleet run")
    resume.add_argument("run")
    resume.add_argument("--approve-major", action="store_true", help=argparse.SUPPRESS)
    _add_llm_args(resume)
    _add_exec_args(resume)
    resume.set_defaults(func=cmd_runs_resume, workers=None)

    up = sub.add_parser("upgrade", help="targeted single-repository upgrade")
    up.add_argument("--repo", required=True, help="local repository path or Git URL (https/ssh)")
    up.add_argument("--base", help="base branch (default: the repository's current/default branch)")
    up.add_argument("--state-dir", help="where remote repositories are cached (default: ./.depbot)")
    up.add_argument("--dependency", required=True)
    up.add_argument("--to", required=True, help="target version")
    up.add_argument("--changelog", help="local changelog/release-notes file for research")
    up.add_argument("--upstream-repo", help="explicit upstream repository (owner/name)")
    up.add_argument("--approve-major", action="store_true")
    up.add_argument("--artifacts", default="artifacts")
    up.add_argument("--conan-home", help="isolated Conan home (e.g. from `depbot demo setup`)")
    up.add_argument("--create-pr", action="store_true", help="open a GitHub PR if verified")
    up.add_argument("--json", action="store_true")
    _add_llm_args(up)
    _add_exec_args(up, workers=False)
    up.set_defaults(func=cmd_upgrade)

    mon = sub.add_parser("monitor", parents=[common], help="poll watched upstreams and process new releases")
    mon.add_argument("--once", action="store_true", help="poll a single time and exit")
    mon.add_argument("--interval", type=float, default=3600.0, help="seconds between polls (default: 3600)")
    mon.add_argument("--dry-run", action="store_true", help="report new releases and selection only")
    mon.add_argument("--approve-major", action="store_true", help="operator approval for major upgrades")
    mon.add_argument("--create-pr", action="store_true", help="open GitHub PRs for verified repos")
    mon.add_argument("--artifacts", default="artifacts", help="artifacts root (default: ./artifacts)")
    _add_llm_args(mon)
    _add_exec_args(mon)
    mon.set_defaults(func=cmd_monitor)

    bench = sub.add_parser("bench", help="reproducible migration benchmarks").add_subparsers(
        dest="bench_command", required=True
    )
    blist = bench.add_parser("list", help="cases in a benchmark registry")
    blist.add_argument("--registry", default="benchmarks/registry.yaml")
    blist.add_argument("--json", action="store_true")
    blist.set_defaults(func=cmd_bench_list)
    bhash = bench.add_parser("hash", help="content hash that pins an in-tree fixture repository")
    bhash.add_argument("path", nargs="+")
    bhash.set_defaults(func=cmd_bench_hash)
    brun = bench.add_parser("run", help="run cases and write results.json + report.md")
    brun.add_argument("--registry", default="benchmarks/registry.yaml")
    brun.add_argument("--split", choices=("dev", "held_out", "all"), default="dev",
                      help="which split to run (default: dev; held-out cases run only when asked)")
    brun.add_argument("--case", action="append", default=[], metavar="ID", help="run only this case (repeatable)")
    brun.add_argument("--out", help="output directory (default: <artifacts>/bench-<timestamp>)")
    brun.add_argument("--artifacts", default="artifacts")
    brun.add_argument("--json", action="store_true")
    _add_llm_args(brun)
    _add_exec_args(brun, workers=False)
    brun.set_defaults(func=cmd_bench_run)
    breport = bench.add_parser("report", help="re-render report.md from a results.json")
    breport.add_argument("results")
    breport.add_argument("--stdout", action="store_true")
    breport.set_defaults(func=cmd_bench_report)

    demo = sub.add_parser("demo", help="demo helpers").add_subparsers(dest="demo_command", required=True)
    setup = demo.add_parser("setup", help="build local recipes into the fleet's isolated Conan home")
    setup.add_argument("--fleet", required=True, help="fleet YAML config")
    setup.add_argument("--state-dir", help="registry state dir (default: <fleet dir>/.depbot)")
    setup.add_argument("--recipe", action="append", required=True,
                       help="Conan recipe directory; repeat for each version")
    setup.set_defaults(func=cmd_demo_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (RegistryError, LLMError, FleetRunError, StoreError, UnknownRun, ExecutionConfigError, BenchmarkError,
            ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
