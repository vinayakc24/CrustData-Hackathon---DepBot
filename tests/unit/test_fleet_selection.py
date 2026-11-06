import subprocess
import textwrap

import pytest

from depbot.cli import main
from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.events import EventLog
from depbot.fleet.registry import RegistryError, load_event, load_fleet
from depbot.fleet.selection import select_repositories
from depbot.fleet.versions import change_level, compare
from depbot.models import ChangeLevel, FleetClassification, OutcomeStatus, ReleaseEvent
from depbot.policy.engine import pre_migration_gate, pre_pr_gate
from depbot.models import UpgradePolicy


def _repo(root, name, requires):
    path = root / name
    path.mkdir()
    if requires is not None:
        (path / "conanfile.txt").write_text(f"[requires]\n{requires}\n")
    return path


def _git_commit(path):
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")


@pytest.fixture
def fleet_dir(tmp_path):
    _repo(tmp_path, "repo_a", "toydep/1.0.0")
    _repo(tmp_path, "repo_b", "toydep/1.0.0")
    _repo(tmp_path, "repo_c", "toydep/2.0.0")
    _repo(tmp_path, "repo_d", "zlib/1.3.1")
    _repo(tmp_path, "repo_e", "toydep/1.0.0")
    (tmp_path / "fleet.yaml").write_text(textwrap.dedent("""\
        fleet_id: test-fleet
        default_policy_id: demo
        policies:
          demo:
            allowed_change_levels: [patch, minor, major]
        repositories:
          - {repository_id: repo_a, repo_url_or_path: repo_a, display_name: A}
          - {repository_id: repo_b, repo_url_or_path: repo_b, display_name: B}
          - {repository_id: repo_c, repo_url_or_path: repo_c, display_name: C}
          - {repository_id: repo_d, repo_url_or_path: repo_d, display_name: D}
          - {repository_id: repo_e, repo_url_or_path: repo_e, display_name: E, enabled: false}
        """))
    (tmp_path / "event.yaml").write_text(textwrap.dedent("""\
        event_id: toydep-v2
        dependency_name: toydep
        upstream_repo: local/toydep
        previous_version: 1.0.0
        target_version: 2.0.0
        changelog_path: CHANGELOG.md
        """))
    return tmp_path


def _select(fleet_dir, approve=True):
    fleet = load_fleet(fleet_dir / "fleet.yaml")
    if approve:
        fleet.config.policies["demo"] = fleet.config.policies["demo"].model_copy(
            update={"major_approved": True}
        )
    event = load_event(fleet_dir / "event.yaml")
    index = DependencyIndex(fleet_dir / ".depbot" / "inventory.json")
    return event, {s.repository.repository_id: s for s in select_repositories(event, fleet, index)}


def test_selection_picks_affected_and_explains_skips(fleet_dir):
    event, sel = _select(fleet_dir)
    assert {k for k, s in sel.items() if s.eligible} == {"repo_a", "repo_b"}
    assert sel["repo_c"].classification == FleetClassification.ALREADY_AT_TARGET_OR_NEWER
    assert sel["repo_d"].classification == FleetClassification.UNAFFECTED
    assert sel["repo_e"].classification == FleetClassification.DISABLED
    out_c = sel["repo_c"].to_outcome(event)
    assert out_c.status == OutcomeStatus.SKIPPED and "already on toydep 2.0.0" in out_c.reason
    assert event.changelog_path.endswith("CHANGELOG.md") and event.changelog_path.startswith("/")


def test_major_upgrade_blocked_without_approval(fleet_dir):
    event, sel = _select(fleet_dir, approve=False)
    assert not sel["repo_a"].eligible
    outcome = sel["repo_a"].to_outcome(event)
    assert outcome.status == OutcomeStatus.BLOCKED and "approval" in outcome.reason


def test_index_rescans_only_when_commit_or_manifest_changes(fleet_dir):
    _git_commit(fleet_dir / "repo_a")
    fleet = load_fleet(fleet_dir / "fleet.yaml")
    repo = fleet.get("repo_a")
    root = fleet.local_root(repo)
    index = DependencyIndex(fleet_dir / "inv.json")
    assert index.refresh(repo, root)[1] is True
    assert index.refresh(repo, root)[1] is False
    (root / "conanfile.txt").write_text("[requires]\ntoydep/1.1.0\n")
    scan, rescanned = index.refresh(repo, root)
    assert rescanned and scan.dependencies[0].current_version == "1.1.0"
    index.save()
    assert DependencyIndex(fleet_dir / "inv.json").get("repo_a").indexed_commit


def test_unsupported_manifest_becomes_blocked_not_crash(fleet_dir):
    (fleet_dir / "repo_d" / "conanfile.txt").unlink()
    (fleet_dir / "repo_d" / "conanfile.py").write_text("")
    event, sel = _select(fleet_dir)
    assert sel["repo_d"].classification == FleetClassification.NEEDS_RESCAN
    assert sel["repo_d"].to_outcome(event).status == OutcomeStatus.BLOCKED


def test_event_dedup_by_id_and_key(tmp_path):
    log = EventLog(tmp_path / "events.json")
    e = ReleaseEvent(event_id="e1", dependency_name="toydep", upstream_repo="u",
                     previous_version="1.0.0", target_version="2.0.0")
    assert log.record(e) is True
    assert log.record(e) is False
    assert log.record(e.model_copy(update={"event_id": "e2"})) is False
    assert EventLog(tmp_path / "events.json").find_duplicate(e) is not None


def test_registry_errors(tmp_path):
    (tmp_path / "f.yaml").write_text("fleet_id: x\nrepositories:\n  - {repository_id: a, repo_url_or_path: a, display_name: A, policy_id: nope}\n")
    with pytest.raises(RegistryError, match="unknown policy_id"):
        load_fleet(tmp_path / "f.yaml")
    with pytest.raises(RegistryError):
        load_fleet(tmp_path / "missing.yaml")


def test_versions_and_policy_gates():
    assert compare("1.0.0", "2.0.0") == -1 and compare("v2.0", "2.0.0") == 0
    assert change_level("1.2.3", "1.2.4") == ChangeLevel.PATCH
    assert change_level("1.2.3", "2.0.0") == ChangeLevel.MAJOR
    assert change_level("abc", "1.0") == ChangeLevel.UNKNOWN
    default = UpgradePolicy()
    assert not pre_migration_gate(default, "1.0.0", "2.0.0").allowed
    assert pre_migration_gate(default, "1.0.0", "1.1.0").allowed
    assert not pre_pr_gate(default, verification_passed=False, tests_ran=True).allowed
    assert not pre_pr_gate(default, verification_passed=True, tests_ran=False).allowed


def test_cli_plan_and_status(fleet_dir, capsys):
    args = ["--fleet", str(fleet_dir / "fleet.yaml")]
    assert main(["fleet", "plan", *args, "--event", str(fleet_dir / "event.yaml"), "--approve-major"]) == 0
    out = capsys.readouterr().out
    assert "2 job(s) would be scheduled: repo_a, repo_b" in out
    assert main(["fleet", "status", *args]) == 0
    assert "toydep/2.0.0 (conan)" in capsys.readouterr().out
    assert main(["fleet", "status", "--fleet", str(fleet_dir / "missing.yaml")]) == 2


def test_scan_cache_key_includes_scanner_version(tmp_path, monkeypatch):
    import depbot.fleet.dependency_index as index

    (tmp_path / "conanfile.txt").write_text("[requires]\ntoydep/1.0.0\n")
    first = index._manifest_digest(tmp_path)
    monkeypatch.setattr(index, "SCANNER_VERSION", "999")
    assert index._manifest_digest(tmp_path) != first  # a parser fix re-scans cached refusals
