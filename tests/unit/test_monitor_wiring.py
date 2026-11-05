"""`depbot monitor` wiring. Only GitHub is mocked (B's get_latest_github_release)."""

import textwrap

import pytest

import depbot.monitor.release_monitor as rm
from depbot.cli import main
from depbot.fleet.monitor import build_watches, poll_fleet_once, run_monitor
from depbot.fleet.registry import RegistryError, load_fleet
from depbot.models import OutcomeStatus
from depbot.research.github_client import GitHubRelease, GitHubReleaseError
from tests.unit.test_fleet_runner import _make_repo
from tests.unit.test_orchestrator import _stages


class FakeGitHub:
    def __init__(self, latest):
        self.latest, self.calls = latest, []  # repo -> tag | Exception

    def __call__(self, *, upstream_repo, **_):
        self.calls.append(upstream_repo)
        value = self.latest[upstream_repo]
        if isinstance(value, Exception):
            raise value
        return GitHubRelease(tag_name=value, name=value, body=f"## {value}\n- `old_sum` was removed; use `sum`.",
                             html_url=f"https://github.com/{upstream_repo}/releases/tag/{value}",
                             published_at="2026-11-01T00:00:00Z", prerelease=False)


@pytest.fixture
def fleet_dir(tmp_path):
    _make_repo(tmp_path, "repo_a", "1.0.0")
    _make_repo(tmp_path, "repo_b", "1.2.0")
    _make_repo(tmp_path, "repo_c", "2.0.0", source="int main(){return sum(2,3)==5?0:1;}\n")
    _make_repo(tmp_path, "repo_off", "0.1.0")
    (tmp_path / "fleet.yaml").write_text(textwrap.dedent("""\
        fleet_id: watched
        default_policy_id: p
        policies: {p: {allowed_change_levels: [patch, minor, major], major_approved: true}}
        watches:
          - {dependency_name: toydep, upstream_repo: acme/toydep}
          - {dependency_name: fmt, upstream_repo: fmtlib/fmt}
        repositories:
          - {repository_id: repo_a, repo_url_or_path: repo_a, display_name: A}
          - {repository_id: repo_b, repo_url_or_path: repo_b, display_name: B}
          - {repository_id: repo_c, repo_url_or_path: repo_c, display_name: C}
          - {repository_id: repo_off, repo_url_or_path: repo_off, display_name: "Disabled repo", enabled: false}
        """))
    return tmp_path


def _poll(fleet_dir, **kw):
    return poll_fleet_once(load_fleet(fleet_dir / "fleet.yaml"), state_dir=fleet_dir / ".depbot",
                           artifacts_root=fleet_dir / "artifacts", stages_factory=_stages, **kw)


def test_watches_use_lowest_enabled_version_and_skip_unused(fleet_dir):
    watches, notes = build_watches(load_fleet(fleet_dir / "fleet.yaml"), state_dir=fleet_dir / ".depbot")
    assert [(w.dependency_name, w.upstream_repo, w.current_version) for w in watches] == [
        ("toydep", "acme/toydep", "1.0.0")]  # repo_off (0.1.0) is disabled
    assert any("watch fmt: no enabled repository declares it" in n for n in notes)


def test_new_release_runs_the_fleet_then_is_deduplicated(fleet_dir, monkeypatch):
    gh = FakeGitHub({"acme/toydep": "v2.0.0"})
    monkeypatch.setattr(rm, "get_latest_github_release", gh)
    report = _poll(fleet_dir)
    assert gh.calls == ["acme/toydep"]  # fmt is not polled: nothing uses it
    (outcome,) = report.events
    assert outcome.action == "processed" and outcome.event.target_version == "2.0.0"
    assert outcome.event.event_id == "github-release:acme/toydep:v2.0.0"
    by_id = {o.repository_id: o.status for o in outcome.summary.outcomes}
    assert by_id == {"repo_a": OutcomeStatus.VERIFIED, "repo_b": OutcomeStatus.VERIFIED,
                     "repo_c": OutcomeStatus.SKIPPED, "repo_off": OutcomeStatus.SKIPPED}
    assert report.ok

    again = _poll(fleet_dir)
    assert [e.action for e in again.events] == ["duplicate"] and again.ok


def test_dry_run_reports_selection_without_recording(fleet_dir, monkeypatch):
    monkeypatch.setattr(rm, "get_latest_github_release", FakeGitHub({"acme/toydep": "2.0.0"}))
    report = _poll(fleet_dir, dry_run=True)
    (outcome,) = report.events
    assert outcome.action == "dry_run" and outcome.jobs == ["repo_a", "repo_b"]
    assert not (fleet_dir / ".depbot" / "events.json").exists() and not (fleet_dir / "artifacts").exists()


def test_no_new_release_and_failing_watch(fleet_dir, monkeypatch):
    (fleet_dir / "repo_d").mkdir()
    (fleet_dir / "repo_d" / "conanfile.txt").write_text("[requires]\nfmt/10.2.1\n")
    cfg = (fleet_dir / "fleet.yaml").read_text() + "  - {repository_id: repo_d, repo_url_or_path: repo_d, display_name: D}\n"
    (fleet_dir / "fleet.yaml").write_text(cfg)
    monkeypatch.setattr(rm, "get_latest_github_release", FakeGitHub({
        "acme/toydep": "1.0.0", "fmtlib/fmt": GitHubReleaseError("GitHub API rate limit exceeded")}))
    report = _poll(fleet_dir)
    assert report.events == [] and not report.ok
    assert report.failures == ["fmt (fmtlib/fmt): could not check fmt at fmtlib/fmt: GitHub API rate limit exceeded"]


def test_fleet_crash_is_isolated(fleet_dir, monkeypatch):
    import depbot.fleet.monitor as mon
    monkeypatch.setattr(rm, "get_latest_github_release", FakeGitHub({"acme/toydep": "2.0.0"}))
    monkeypatch.setattr(mon, "run_fleet", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    report = _poll(fleet_dir)
    assert [(e.action, e.detail) for e in report.events] == [("failed", "OSError: disk full")] and not report.ok


def test_monitor_loop_recomputes_and_stops(fleet_dir, monkeypatch):
    gh = FakeGitHub({"acme/toydep": "1.0.0"})
    monkeypatch.setattr(rm, "get_latest_github_release", gh)

    class StopAfter:
        def __init__(self, n):
            self.n, self.waits = n, []

        def is_set(self):
            return len(self.waits) >= self.n

        def wait(self, timeout):
            self.waits.append(timeout)
            return len(self.waits) >= self.n

    reports, stop = [], StopAfter(2)
    run_monitor(load_fleet(fleet_dir / "fleet.yaml"), interval_seconds=120, stop=stop, on_report=reports.append,
                state_dir=fleet_dir / ".depbot", artifacts_root=fleet_dir / "artifacts", stages_factory=_stages)
    assert len(reports) == 2 and stop.waits == [120, 120] and len(gh.calls) == 2
    with pytest.raises(ValueError, match="at least 60"):
        run_monitor(load_fleet(fleet_dir / "fleet.yaml"), interval_seconds=5, stop=stop, on_report=print,
                    state_dir=fleet_dir / ".depbot", artifacts_root=fleet_dir, stages_factory=_stages)


def test_watch_config_validation(tmp_path):
    for bad, match in (("upstream_repo: not-a-slug", "owner/name"),
                       ("upstream_repo: a/b/c", "owner/name")):
        (tmp_path / "f.yaml").write_text(f"fleet_id: f\nrepositories: []\nwatches:\n  - {{dependency_name: d, {bad}}}\n")
        with pytest.raises(RegistryError, match=match):
            load_fleet(tmp_path / "f.yaml")
    (tmp_path / "f.yaml").write_text("fleet_id: f\nrepositories: []\nwatches:\n"
                                     "  - {dependency_name: d, upstream_repo: a/b}\n  - {dependency_name: d, upstream_repo: a/c}\n")
    with pytest.raises(RegistryError, match="watched only once"):
        load_fleet(tmp_path / "f.yaml")


def test_cli_monitor_once(fleet_dir, monkeypatch, capsys):
    monkeypatch.setattr(rm, "get_latest_github_release", FakeGitHub({"acme/toydep": "2.0.0"}))
    args = ["monitor", "--fleet", str(fleet_dir / "fleet.yaml"), "--once", "--dry-run"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "polled 1 watch(es): toydep@1.0.0 <- acme/toydep" in out
    assert "DRY_RUN   github-release:acme/toydep:2.0.0 (toydep 1.0.0 -> 2.0.0): would run jobs for repo_a, repo_b" in out
    (fleet_dir / "plain.yaml").write_text("fleet_id: p\nrepositories: []\n")
    assert main(["monitor", "--fleet", str(fleet_dir / "plain.yaml"), "--once"]) == 2
    assert "no `watches:`" in capsys.readouterr().err
