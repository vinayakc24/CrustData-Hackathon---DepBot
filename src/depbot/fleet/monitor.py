"""`depbot monitor`: poll watched upstream projects and run the fleet for new releases.

Owner: Member A (wiring). Polling itself is Member B's ``depbot.monitor.release_monitor``
and is used unchanged.

Each poll cycle:
1. Build watches from the fleet config. Each watch's current version is the **lowest**
   version any enabled repository declares (from the dependency inventory, re-read
   when repositories change; remote repositories are synced first). Watches no
   repository uses are skipped, with a note.
2. ``poll_once`` asks GitHub for each upstream's latest stable release (B's module).
3. Each new release becomes a ``ReleaseEvent``. Already-processed events are skipped
   by the EventLog duplicate check. New events go through ``run_fleet``, the same
   path as ``depbot run``. With ``dry_run`` only the selection is reported.

Watches are recomputed every cycle, so a repository upgraded since the last poll is
seen at its new version. One failing watch or fleet run never stops the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.events import EventLog
from depbot.fleet.registry import Fleet, RegistryError
from depbot.fleet.runner import DuplicateEvent, run_fleet
from depbot.fleet.selection import select_repositories
from depbot.fleet.versions import UnparseableVersion, compare
from depbot.interfaces import PipelineStages
from depbot.llm.runtime import LLMFactory
from depbot.models import FleetRunSummary, ReleaseEvent
from depbot.monitor.release_monitor import ReleaseWatch, poll_once

log = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 60  # be polite to the GitHub API (anonymous limit: 60 requests/hour)


class _StopEvent(Protocol):
    def is_set(self) -> bool: ...
    def wait(self, timeout: float) -> bool: ...


class EventOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: ReleaseEvent
    action: str  # "processed" | "duplicate" | "dry_run" | "failed"
    detail: str = ""
    summary: FleetRunSummary | None = None
    jobs: list[str] = Field(default_factory=list)  # dry run: repositories that would get a job


class PollReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watches: list[str] = Field(default_factory=list)  # "fmt@10.2.1 <- fmtlib/fmt"
    notes: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    events: list[EventOutcome] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and all(
            e.action != "failed"
            and (e.summary is None or e.summary.repositories_failed_or_blocked == 0)
            for e in self.events
        )


def build_watches(fleet: Fleet, *, state_dir: Path) -> tuple[list[ReleaseWatch], list[str]]:
    """Watches with the lowest declared version per watched dependency."""
    index = DependencyIndex(state_dir / "inventory.json")
    lowest: dict[str, str] = {}
    notes: list[str] = []
    watched = {w.dependency_name for w in fleet.config.watches if w.enabled}
    for repo in fleet.repositories:
        if not repo.enabled:
            continue
        try:
            root, _ = fleet.resolve_root(repo, state_dir / "sources")
        except RegistryError as exc:
            notes.append(f"{repo.repository_id}: skipped for version discovery ({exc})")
            continue
        if not root.is_dir():
            notes.append(f"{repo.repository_id}: path not found: {root}")
            continue
        scan, _ = index.refresh(repo, root)
        for dep in scan.dependencies:
            if dep.dependency_name not in watched:
                continue
            current = lowest.get(dep.dependency_name)
            try:
                if current is None or compare(dep.current_version, current) < 0:
                    lowest[dep.dependency_name] = dep.current_version
            except UnparseableVersion:
                notes.append(f"{repo.repository_id}: {dep.dependency_name} version "
                             f"{dep.current_version!r} is not comparable; ignored for watching")
    index.save()

    watches: list[ReleaseWatch] = []
    for w in fleet.config.watches:
        if not w.enabled:
            continue
        if w.dependency_name not in lowest:
            notes.append(f"watch {w.dependency_name}: no enabled repository declares it; not polled")
            continue
        watches.append(ReleaseWatch(w.dependency_name, w.upstream_repo.strip().strip("/"), lowest[w.dependency_name]))
    return watches, notes


def poll_fleet_once(
    fleet: Fleet,
    *,
    state_dir: Path,
    artifacts_root: Path,
    stages_factory: Callable[[], PipelineStages],
    create_pr: bool = False,
    llm_factory: LLMFactory | None = None,
    dry_run: bool = False,
) -> PollReport:
    report = PollReport()
    watches, report.notes = build_watches(fleet, state_dir=state_dir)
    report.watches = [f"{w.dependency_name}@{w.current_version} <- {w.upstream_repo}" for w in watches]
    if not watches:
        return report
    result = poll_once(watches)
    report.failures = [f"{f.watch.dependency_name} ({f.watch.upstream_repo}): {f.error}" for f in result.failures]
    events_log = EventLog(state_dir / "events.json")
    for event in result.events:
        duplicate = events_log.find_duplicate(event)
        if duplicate is not None:
            report.events.append(EventOutcome(event=event, action="duplicate",
                                              detail=f"already processed as {duplicate.event_id}"))
            continue
        if dry_run:
            selections = select_repositories(event, fleet, DependencyIndex(state_dir / "inventory.json"),
                                             sources_dir=state_dir / "sources")
            report.events.append(EventOutcome(
                event=event, action="dry_run", detail="selection only; nothing recorded or run",
                jobs=[s.repository.repository_id for s in selections if s.eligible]))
            continue
        try:
            summary = run_fleet(event, fleet, stages=stages_factory(), state_dir=state_dir,
                                artifacts_root=artifacts_root, create_pr=create_pr, llm_factory=llm_factory)
        except DuplicateEvent as exc:  # raced with another run
            report.events.append(EventOutcome(event=event, action="duplicate", detail=str(exc)))
            continue
        except Exception as exc:  # isolate: one release must not stop the monitor
            log.exception("fleet run for %s failed", event.event_id)
            report.events.append(EventOutcome(event=event, action="failed", detail=f"{type(exc).__name__}: {exc}"))
            continue
        report.events.append(EventOutcome(event=event, action="processed", summary=summary,
                                          detail=summary.summary_path or ""))
    return report


def run_monitor(
    fleet: Fleet,
    *,
    interval_seconds: float,
    stop: _StopEvent,
    on_report: Callable[[PollReport], None],
    **poll_kwargs,
) -> None:
    """Poll immediately, then every ``interval_seconds`` until ``stop`` is set."""
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS} seconds")
    while not stop.is_set():
        on_report(poll_fleet_once(fleet, **poll_kwargs))
        if stop.wait(interval_seconds):
            return
