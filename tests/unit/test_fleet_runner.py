import json
import textwrap

import pytest

from depbot.cli import main
from depbot.fleet.registry import load_event, load_fleet
from depbot.fleet.runner import DuplicateEvent, run_fleet
from depbot.models import OutcomeStatus
from tests.unit.test_orchestrator import MAIN, _stages


def _make_repo(root, name, version, source=MAIN):
    repo = root / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.cpp").write_text(source)
    (repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\nproject(x CXX)\n")
    (repo / "conanfile.txt").write_text(f"[requires]\ntoydep/{version}\n")


@pytest.fixture
def fleet_dir(tmp_path):
    _make_repo(tmp_path, "repo_a", "1.0.0")
    _make_repo(tmp_path, "repo_b", "1.0.0")
    _make_repo(tmp_path, "repo_c", "2.0.0", source="int main(){return sum(2,3)==5?0:1;}\n")
    (tmp_path / "fleet.yaml").write_text(textwrap.dedent("""\
        fleet_id: test-fleet
        default_policy_id: demo
        policies:
          demo: {allowed_change_levels: [patch, minor, major], major_approved: true}
        repositories:
          - {repository_id: repo_a, repo_url_or_path: repo_a, display_name: A}
          - {repository_id: repo_b, repo_url_or_path: repo_b, display_name: B}
          - {repository_id: repo_c, repo_url_or_path: repo_c, display_name: C}
        """))
    (tmp_path / "event.yaml").write_text(textwrap.dedent("""\
        event_id: toydep-v2
        dependency_name: toydep
        upstream_repo: local/toydep
        previous_version: 1.0.0
        target_version: 2.0.0
        """))
    return tmp_path


def _run(fleet_dir, stages, **kw):
    return run_fleet(
        load_event(fleet_dir / "event.yaml"), load_fleet(fleet_dir / "fleet.yaml"),
        stages=stages, state_dir=fleet_dir / ".depbot", artifacts_root=fleet_dir / "artifacts", **kw,
    )


def test_fleet_run_migrates_two_and_skips_one(fleet_dir):
    calls = []

    def research(**kw):
        calls.append(kw["dependency"].name)
        return _stages().research(**kw)

    summary = _run(fleet_dir, _stages(research=research))
    by_id = {o.repository_id: o for o in summary.outcomes}
    assert by_id["repo_a"].status == OutcomeStatus.VERIFIED
    assert by_id["repo_b"].status == OutcomeStatus.VERIFIED
    assert by_id["repo_c"].status == OutcomeStatus.SKIPPED
    assert len(calls) == 2  # repo_c never reached research/LLM/build
    assert (summary.repositories_considered, summary.repositories_affected,
            summary.repositories_verified, summary.repositories_skipped) == (3, 2, 2, 1)
    assert by_id["repo_a"].run_id != by_id["repo_b"].run_id  # isolated jobs
    md = open(summary.summary_path).read()
    assert "| repo_c | SKIPPED | already_at_target_or_newer" in md
    data = json.loads((fleet_dir / "artifacts").glob("fleet-*/summary.json").__next__().read_text())
    assert data["repositories_verified"] == 2


def test_one_crashing_repo_does_not_stop_the_fleet(fleet_dir):
    base = _stages()

    def flaky_plan(**kw):
        if any(u.file_path == "src/main.cpp" for u in kw["usages"]) and not hasattr(flaky_plan, "done"):
            flaky_plan.done = True
            raise RuntimeError("planner exploded")
        return base.plan(**kw)

    summary = _run(fleet_dir, _stages(plan=flaky_plan))
    statuses = sorted(o.status.value for o in summary.outcomes)
    assert statuses == ["failed", "skipped", "verified"]
    assert summary.repositories_failed_or_blocked == 1


def test_duplicate_event_refused_unless_forced(fleet_dir):
    _run(fleet_dir, _stages())
    with pytest.raises(DuplicateEvent):
        _run(fleet_dir, _stages())
    assert _run(fleet_dir, _stages(), force=True).repositories_verified == 2


def test_cli_run_never_reports_unverified_success(fleet_dir, capsys):
    # These dummy repos cannot build (no toydep package): real stages must block, not pass.
    code = main(["run", "--fleet", str(fleet_dir / "fleet.yaml"), "--event", str(fleet_dir / "event.yaml"),
                 "--artifacts", str(fleet_dir / "artifacts")])
    out = capsys.readouterr().out
    assert code == 1 and "BLOCKED" in out and "SKIPPED" in out and "VERIFIED" not in out
    assert main(["run", "--fleet", str(fleet_dir / "fleet.yaml"), "--event", str(fleet_dir / "event.yaml"),
                 "--artifacts", str(fleet_dir / "artifacts")]) == 3  # duplicate


def test_cli_upgrade_policy_and_noop(fleet_dir, capsys):
    args = ["upgrade", "--repo", str(fleet_dir / "repo_a"), "--dependency", "toydep", "--to", "2.0.0",
            "--artifacts", str(fleet_dir / "art")]
    assert main(args) == 1 and "major upgrades are not allowed" in capsys.readouterr().err
    assert main(args + ["--approve-major"]) == 1  # real stages: dummy repo cannot verify
    assert capsys.readouterr().out.startswith("BLOCKED")
    assert main(["upgrade", "--repo", str(fleet_dir / "repo_c"), "--dependency", "toydep",
                 "--to", "2.0.0"]) == 0
    assert "nothing to do" in capsys.readouterr().out
