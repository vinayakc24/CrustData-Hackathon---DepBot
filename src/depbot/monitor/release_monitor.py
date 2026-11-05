"""Minimal scheduled polling for explicitly configured GitHub release watches."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Protocol

from depbot.fleet.versions import UnparseableVersion, compare
from depbot.models import ReleaseEvent
from depbot.research.github_client import (
    GitHubReleaseError,
    get_latest_github_release,
)

MAX_INTERVAL_SECONDS = 30 * 24 * 60 * 60


class ReleaseMonitorError(RuntimeError):
    """A configured release watch could not be evaluated safely."""


@dataclass(frozen=True)
class ReleaseWatch:
    dependency_name: str
    upstream_repo: str
    current_version: str


@dataclass(frozen=True)
class WatchFailure:
    watch: ReleaseWatch
    error: str


@dataclass(frozen=True)
class PollResult:
    events: tuple[ReleaseEvent, ...]
    failures: tuple[WatchFailure, ...]


class _StopEvent(Protocol):
    def wait(self, timeout: float) -> bool: ...


def _normalize_version(version: str) -> str:
    return version[1:] if version.startswith("v") else version


def poll_release_watch(watch: ReleaseWatch) -> ReleaseEvent | None:
    """Return an event only when an explicitly watched repo has a newer release."""
    try:
        release = get_latest_github_release(upstream_repo=watch.upstream_repo)
    except GitHubReleaseError as exc:
        raise ReleaseMonitorError(
            f"could not check {watch.dependency_name} at {watch.upstream_repo}: {exc}"
        ) from exc
    if release.prerelease:
        raise ReleaseMonitorError(
            f"GitHub returned a prerelease for {watch.dependency_name} at {watch.upstream_repo}"
        )

    current = _normalize_version(watch.current_version)
    target = _normalize_version(release.tag_name)
    try:
        is_newer = compare(current, target) < 0
    except UnparseableVersion as exc:
        raise ReleaseMonitorError(
            f"cannot compare release versions for {watch.dependency_name} "
            f"({watch.current_version!r} -> {release.tag_name!r}): {exc}"
        ) from exc
    if not is_newer:
        return None

    repo_identity = watch.upstream_repo.strip().rstrip("/")
    event_id = f"github-release:{repo_identity}:{release.tag_name}"
    try:
        return ReleaseEvent(
            event_id=event_id,
            dependency_name=watch.dependency_name,
            upstream_repo=watch.upstream_repo,
            previous_version=current,
            target_version=target,
            source_url=release.html_url,
        )
    except (TypeError, ValueError) as exc:
        raise ReleaseMonitorError(f"could not normalize release for {watch.dependency_name}: {exc}") from exc


def poll_once(watches: list[ReleaseWatch]) -> PollResult:
    """Poll all watches independently, retaining successes alongside failures."""
    events: list[ReleaseEvent] = []
    failures: list[WatchFailure] = []
    for watch in watches:
        try:
            event = poll_release_watch(watch)
        except ReleaseMonitorError as exc:
            failures.append(WatchFailure(watch=watch, error=str(exc)))
            continue
        if event is not None:
            events.append(event)
    return PollResult(events=tuple(events), failures=tuple(failures))


def _validate_interval(interval_seconds: float) -> None:
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or interval_seconds <= 0
        or interval_seconds > MAX_INTERVAL_SECONDS
    ):
        raise ValueError(
            f"interval_seconds must be positive and no greater than {MAX_INTERVAL_SECONDS}"
        )


def run_polling_loop(
    *,
    watches: list[ReleaseWatch],
    interval_seconds: float,
    on_events: Callable[[list[ReleaseEvent]], None],
    stop_event: _StopEvent,
    on_errors: Callable[[list[WatchFailure]], None] | None = None,
) -> None:
    """Poll immediately, dispatch notifications, and stop through ``stop_event``.

    The caller remains responsible for EventLog deduplication and fleet dispatch.
    This monitor never starts migration work or creates unmanaged processes.
    """
    _validate_interval(interval_seconds)
    while True:
        result = poll_once(watches)
        if result.events:
            on_events(list(result.events))
        if result.failures and on_errors is not None:
            on_errors(list(result.failures))
        if stop_event.wait(interval_seconds):
            return
