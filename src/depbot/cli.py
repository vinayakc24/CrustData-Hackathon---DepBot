"""DepBot command-line interface (owner: Member A).

    depbot fleet status --fleet examples/demo-fleet.yaml
    depbot fleet plan   --fleet examples/demo-fleet.yaml --event examples/events/toydep-v2.yaml
    depbot run          --fleet examples/demo-fleet.yaml --event examples/events/toydep-v2.yaml --approve-major
    depbot upgrade      --repo PATH --dependency toydep --to 2.0.0 --approve-major
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from depbot.fleet.dependency_index import DependencyIndex, scan_repository
from depbot.fleet.events import EventLog
from depbot.fleet.registry import Fleet, RegistryError, load_event, load_fleet
from depbot.fleet.runner import DuplicateEvent, run_fleet
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
from depbot.orchestrator import load_default_stages, run_repository_job
from depbot.package_managers.conan import CONAN_HOME_ENV
from depbot.package_managers.conan_setup import SetupError, prepare_local_packages
from depbot.policy.engine import pre_migration_gate

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
            root = fleet.local_root(repo)
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
    selections = select_repositories(event, fleet, DependencyIndex(state_dir / "inventory.json"))
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


def cmd_run(args: argparse.Namespace) -> int:
    """Process one release event across the fleet. Exit 0 only if no job failed/blocked."""
    fleet, state_dir = _load(args)
    event = load_event(Path(args.event))
    try:
        summary = run_fleet(
            event,
            fleet,
            stages=load_default_stages(),
            state_dir=state_dir,
            artifacts_root=Path(args.artifacts).resolve(),
            force=args.force,
            create_pr=args.create_pr,
        )
    except DuplicateEvent as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(summary.model_dump_json(indent=2))
    else:
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
    return 0 if all(o.status in _GOOD for o in summary.outcomes) else 1


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Targeted single-repository upgrade through the same migration engine."""
    root = Path(args.repo).resolve()
    if args.conan_home:
        _use_conan_home(Path(args.conan_home))
    repo = RepositoryRecord(repository_id=root.name, repo_url_or_path=str(root), display_name=root.name)
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
    state, outcome = run_repository_job(job, load_default_stages(), artifacts_root=Path(args.artifacts).resolve())
    if args.json:
        print(outcome.model_dump_json(indent=2))
    else:
        print(f"{outcome.status.value.upper()}: {outcome.reason or ''}")
        print(f"artifacts: {state.artifacts_dir}")
    return 0 if outcome.status in _GOOD else 1


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
    run.set_defaults(func=cmd_run)

    up = sub.add_parser("upgrade", help="targeted single-repository upgrade")
    up.add_argument("--repo", required=True, help="local repository path")
    up.add_argument("--dependency", required=True)
    up.add_argument("--to", required=True, help="target version")
    up.add_argument("--changelog", help="local changelog/release-notes file for research")
    up.add_argument("--upstream-repo", help="explicit upstream repository (owner/name)")
    up.add_argument("--approve-major", action="store_true")
    up.add_argument("--artifacts", default="artifacts")
    up.add_argument("--conan-home", help="isolated Conan home (e.g. from `depbot demo setup`)")
    up.add_argument("--create-pr", action="store_true", help="open a GitHub PR if verified")
    up.add_argument("--json", action="store_true")
    up.set_defaults(func=cmd_upgrade)

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
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
