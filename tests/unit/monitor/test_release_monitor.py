from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from depbot.models import ReleaseEvent
from depbot.monitor import release_monitor as monitor
from depbot.monitor.release_monitor import (
    PollResult,
    ReleaseMonitorError,
    ReleaseWatch,
    WatchFailure,
    poll_once,
    poll_release_watch,
    run_polling_loop,
)
from depbot.research.github_client import GitHubRelease, GitHubReleaseError


def _release(tag: str, *, prerelease: bool = False) -> GitHubRelease:
    return GitHubRelease(
        tag_name=tag,
        name=f"Release {tag}",
        body="Notes",
        html_url=f"https://github.com/acme/{tag}/releases/tag/{tag}",
        published_at="2026-11-06T01:00:00Z",
        prerelease=prerelease,
    )


def _watch(current: str = "1.0.0", repo: str = "acme/widget") -> ReleaseWatch:
    return ReleaseWatch("widget", repo, current)


def _mock_release(monkeypatch, release: GitHubRelease) -> None:
    monkeypatch.setattr(monitor, "get_latest_github_release", lambda **kwargs: release)


def test_new_release_creates_normalized_deterministic_event(monkeypatch) -> None:
    release = _release("v2.4.1")
    _mock_release(monkeypatch, release)
    watch = _watch("v1.2.3")

    event = poll_release_watch(watch)

    assert event is not None
    assert event.event_id == "github-release:acme/widget:v2.4.1"
    assert event.dependency_name == "widget"
    assert event.upstream_repo == "acme/widget"
    assert event.previous_version == "1.2.3"
    assert event.target_version == "2.4.1"
    assert event.source_url == release.html_url
    assert poll_release_watch(watch).event_id == event.event_id
    with pytest.raises(FrozenInstanceError):
        watch.current_version = "9.9.9"


@pytest.mark.parametrize("current, tag", [("2.0.0", "v2.0.0"), ("3.0.0", "v2.0.0")])
def test_equal_or_older_release_produces_no_event(monkeypatch, current: str, tag: str) -> None:
    _mock_release(monkeypatch, _release(tag))

    assert poll_release_watch(_watch(current)) is None


def test_normalizes_only_leading_version_prefix(monkeypatch) -> None:
    release = _release("v2.0.0+meta.1")
    _mock_release(monkeypatch, release)
    event = poll_release_watch(_watch("v1.0.0"))

    assert event is not None
    assert event.target_version == "2.0.0+meta.1"
    assert event.source_url == release.html_url


def test_unparseable_current_or_release_version_is_clear(monkeypatch) -> None:
    _mock_release(monkeypatch, _release("latest"))
    with pytest.raises(ReleaseMonitorError, match="cannot compare release versions"):
        poll_release_watch(_watch("1.0.0"))

    _mock_release(monkeypatch, _release("2.0.0"))
    with pytest.raises(ReleaseMonitorError, match="cannot compare release versions"):
        poll_release_watch(_watch("main"))


def test_poll_release_watch_rejects_prerelease_even_if_provider_returns_one(monkeypatch) -> None:
    _mock_release(monkeypatch, _release("v2.0.0-rc1", prerelease=True))

    with pytest.raises(ReleaseMonitorError, match="prerelease"):
        poll_release_watch(_watch())


def test_poll_once_keeps_other_watch_success_when_one_upstream_fails(monkeypatch) -> None:
    releases = {"acme/good": _release("v2.0.0")}

    def latest(*, upstream_repo: str) -> GitHubRelease:
        if upstream_repo not in releases:
            raise GitHubReleaseError("API unavailable")
        return releases[upstream_repo]

    monkeypatch.setattr(monitor, "get_latest_github_release", latest)
    result = poll_once([_watch(repo="acme/broken"), _watch(repo="acme/good")])

    assert len(result.events) == 1
    assert result.events[0].upstream_repo == "acme/good"
    assert len(result.failures) == 1
    assert result.failures[0].watch.upstream_repo == "acme/broken"
    assert "API unavailable" in result.failures[0].error


class _StopAfter:
    def __init__(self, waits: int) -> None:
        self.waits = waits
        self.calls: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.calls.append(timeout)
        return len(self.calls) >= self.waits


def test_polling_starts_immediately_repeats_and_only_calls_event_callback_when_nonempty(monkeypatch) -> None:
    event = ReleaseEvent(
        event_id="e1", dependency_name="widget", upstream_repo="acme/widget",
        target_version="2.0.0",
    )
    failure = WatchFailure(_watch(repo="acme/failing"), "temporary network issue")
    results = iter((PollResult((), (failure,)), PollResult((event,), ())))
    poll_calls: list[int] = []

    def fake_poll(_watches):
        poll_calls.append(len(poll_calls))
        return next(results)

    monkeypatch.setattr(monitor, "poll_once", fake_poll)
    stop = _StopAfter(waits=2)
    emitted: list[list[ReleaseEvent]] = []
    errors: list[list[WatchFailure]] = []
    run_polling_loop(
        watches=[_watch()],
        interval_seconds=17,
        on_events=emitted.append,
        on_errors=errors.append,
        stop_event=stop,
    )

    assert len(poll_calls) == 2
    assert stop.calls == [17, 17]
    assert emitted == [[event]]
    assert errors == [[failure]]


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), monitor.MAX_INTERVAL_SECONDS + 1])
def test_polling_interval_must_be_positive_finite_and_reasonable(interval: float) -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        run_polling_loop(
            watches=[], interval_seconds=interval, on_events=lambda _: None, stop_event=_StopAfter(1)
        )


def test_polling_stop_event_stops_after_immediate_first_poll(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(monitor, "poll_once", lambda watches: calls.append(watches) or PollResult((), ()))
    stop = _StopAfter(waits=1)

    run_polling_loop(
        watches=[], interval_seconds=5, on_events=lambda _: None, stop_event=stop
    )

    assert calls == [[]]
    assert stop.calls == [5]
    assert list(tmp_path.iterdir()) == []
