"""DepBot command-line interface (owner: Member A).

    depbot fleet status --fleet examples/demo-fleet.yaml
    depbot fleet plan   --fleet examples/demo-fleet.yaml --event examples/events/toydep-v2.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.events import EventLog
from depbot.fleet.registry import Fleet, RegistryError, load_event, load_fleet
from depbot.fleet.selection import RepositorySelection, select_repositories, state_dir_for

log = logging.getLogger("depbot")


def _load(args: argparse.Namespace) -> tuple[Fleet, Path]:
    fleet = load_fleet(Path(args.fleet))
    if getattr(args, "approve_major", False):
        for pid, policy in fleet.config.policies.items():
            fleet.config.policies[pid] = policy.model_copy(update={"major_approved": True})
    state_dir = state_dir_for(fleet, Path(args.state_dir) if args.state_dir else None)
    return fleet, state_dir


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
