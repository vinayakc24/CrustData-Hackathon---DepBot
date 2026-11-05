"""Fleet orchestrator: one ReleaseEvent → selection → isolated jobs → fleet summary.

Owner: Member A. Decides *which* repositories run; ``depbot.orchestrator``
decides *how* one repository is upgraded. A crash in one job becomes that
repository's FAILED outcome and never stops the rest of the fleet.
Execution is sequential (P0 fallback); every job still uses its own working copy.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.events import EventLog
from depbot.fleet.registry import Fleet
from depbot.fleet.report import render_fleet_markdown
from depbot.fleet.selection import RepositorySelection, select_repositories
from depbot.interfaces import PipelineStages
from depbot.models import (
    FleetRunSummary,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryUpgradeJob,
    RepositoryUpgradeOutcome,
)
from depbot.orchestrator import run_repository_job

log = logging.getLogger(__name__)


class DuplicateEvent(RuntimeError):
    pass


def _job_for(event: ReleaseEvent, sel: RepositorySelection, fleet: Fleet) -> RepositoryUpgradeJob:
    assert sel.dependency_record is not None and sel.repo_root is not None
    return RepositoryUpgradeJob(
        job_id=f"{event.event_id}:{sel.repository.repository_id}",
        event=event,
        repository=sel.repository,
        dependency_record=sel.dependency_record,
        policy=fleet.policy_for(sel.repository),
        repo_root=sel.repo_root,
    )


def _run_one(
    event: ReleaseEvent, sel: RepositorySelection, fleet: Fleet, stages: PipelineStages, artifacts_root: Path
) -> RepositoryUpgradeOutcome:
    try:
        _, outcome = run_repository_job(_job_for(event, sel, fleet), stages, artifacts_root=artifacts_root)
        return outcome
    except Exception as exc:  # isolate: never let one repository stop the fleet
        log.exception("fleet job for %s crashed", sel.repository.repository_id)
        return RepositoryUpgradeOutcome(
            repository_id=sel.repository.repository_id,
            status=OutcomeStatus.FAILED,
            classification=sel.classification,
            reason=f"job crashed before producing an outcome: {type(exc).__name__}: {exc}",
            dependency_name=event.dependency_name,
            from_version=sel.dependency_record.current_version if sel.dependency_record else None,
            to_version=event.target_version,
        )


def run_fleet(
    event: ReleaseEvent,
    fleet: Fleet,
    *,
    stages: PipelineStages,
    state_dir: Path,
    artifacts_root: Path,
    force: bool = False,
) -> FleetRunSummary:
    events = EventLog(state_dir / "events.json")
    duplicate = events.find_duplicate(event)
    if duplicate is not None and not force:
        raise DuplicateEvent(
            f"event already processed as {duplicate.event_id!r}; use --force to reprocess"
        )
    if duplicate is None:
        events.record(event)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fleet_dir = (artifacts_root / f"fleet-{event.event_id}-{stamp}-{secrets.token_hex(3)}").resolve()
    fleet_dir.mkdir(parents=True, exist_ok=False)

    selections = select_repositories(event, fleet, DependencyIndex(state_dir / "inventory.json"))
    (fleet_dir / "selection.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in selections], indent=2) + "\n", encoding="utf-8"
    )
    outcomes: list[RepositoryUpgradeOutcome] = []
    for sel in selections:
        if sel.eligible:
            log.info("running job for %s", sel.repository.repository_id)
            outcomes.append(_run_one(event, sel, fleet, stages, artifacts_root))
        else:
            outcomes.append(sel.to_outcome(event))

    summary = FleetRunSummary.from_outcomes(event, outcomes)
    summary = summary.model_copy(update={"summary_path": str(fleet_dir / "summary.md")})
    (fleet_dir / "summary.json").write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (fleet_dir / "summary.md").write_text(render_fleet_markdown(summary, fleet.fleet_id), encoding="utf-8")
    return summary
