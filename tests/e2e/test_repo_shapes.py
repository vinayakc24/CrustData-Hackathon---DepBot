"""Real Conan/CMake/CTest: one toydep 1.0.0 -> 2.0.0 event across eleven repository shapes
(examples/shapes). Each shape asserts what a correct DepBot does, including safe refusals."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from depbot.fleet.registry import Fleet, FleetConfig
from depbot.fleet.runner import run_fleet
from depbot.models import ChangeLevel, ReleaseEvent, RepositoryRecord, UpgradePolicy
from depbot.orchestrator import load_default_stages
from depbot.package_managers.conan import CONAN_HOME_ENV
from depbot.package_managers.conan_setup import prepare_local_packages

ROOT = Path(__file__).resolve().parents[2]
TOYDEP = ROOT / "tests" / "fixtures" / "toydep"
SHAPES = ROOT / "examples" / "shapes"

pytestmark = pytest.mark.skipif(shutil.which("conan") is None or shutil.which("cmake") is None,
                                reason="Conan and CMake must be on PATH")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("shapes")
    home = tmp / "conan"
    steps = prepare_local_packages([TOYDEP / "v1", TOYDEP / "v2", SHAPES / "recipes" / "otherdep"], home)
    assert all(s.succeeded for s in steps), [f"{s.label}: {s.output_tail}" for s in steps]
    sources = tmp / "sources"
    shutil.copytree(SHAPES / "repos", sources)
    env = {"CONAN_HOME": str(home), "PATH": __import__("os").environ["PATH"]}
    lock = sources / "s02_lockfile" / "conan.lock"
    lock.unlink()  # re-lock against this machine's cache
    subprocess.run(["conan", "lock", "create", ".", "--lockfile-out=conan.lock"], cwd=lock.parent, env=env,
                   check=True, capture_output=True)
    git = ["git", "-c", "user.name=f", "-c", "user.email=f@f"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=sources, check=True)
    subprocess.run([*git, "add", "-A"], cwd=sources, check=True)
    subprocess.run([*git, "commit", "-qm", "shapes"], cwd=sources, check=True)
    policy = UpgradePolicy(allowed_change_levels=[ChangeLevel.MAJOR], require_human_approval_for_major=False)
    repos = [RepositoryRecord(repository_id=p.name, repo_url_or_path=str(p), display_name=p.name)
             for p in sorted(sources.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    fleet = Fleet(FleetConfig(fleet_id="shapes", repositories=repos, policies={"default": policy}), tmp)
    event = ReleaseEvent(event_id="toydep-v2-shapes", dependency_name="toydep", upstream_repo="local/toydep",
                         previous_version="1.0.0", target_version="2.0.0",
                         changelog_path=str(TOYDEP / "release-notes-2.0.0.md"))
    mp = pytest.MonkeyPatch()
    mp.setenv(CONAN_HOME_ENV, str(home))
    try:
        summary = run_fleet(event, fleet, stages=load_default_stages(), state_dir=tmp / "state",
                            artifacts_root=tmp / "artifacts")
    finally:
        mp.undo()
    out = {}
    for o in summary.outcomes:
        state_path = Path(o.report_path).parent / "state.json" if o.report_path else None
        state = json.loads(state_path.read_text()) if state_path and state_path.is_file() else {}
        out[o.repository_id] = (o, state)
    return out


def _wt(state) -> Path:
    return Path(state["worktree_path"])


def test_expected_outcome_for_every_shape(run):
    expected = json.loads((SHAPES / "expected.json").read_text())
    assert set(run) == set(expected)
    status = {name: o.status.value for name, (o, _s) in run.items()}
    assert status == {
        "s01_multi_requires": "verified", "s02_lockfile": "verified", "s03_version_range": "blocked",
        "s04_conanfile_py": "blocked", "s05_header_and_tests": "verified", "s06_no_tests": "blocked",
        "s07_baseline_broken": "blocked", "s08_crlf": "verified", "s09_no_code_usage": "unaffected",
        "s10_comments_and_strings": "unaffected", "s11_using_declaration": "verified",
    }


def test_multi_requires_changes_only_the_target_line(run):
    text = (_wt(run["s01_multi_requires"][1]) / "conanfile.txt").read_text()
    assert "otherdep/1.0.0" in text and "toydep/2.0.0" in text and "# the adder" in text


def test_lockfile_is_updated_and_part_of_the_change(run):
    _o, state = run["s02_lockfile"]
    lock = json.loads((_wt(state) / "conan.lock").read_text())
    assert [r.split("#")[0] for r in lock["requires"]] == ["toydep/2.0.0"]
    assert "conan.lock" in state["changed_files"]


def test_refusals_are_explained(run):
    assert "version range" in run["s03_version_range"][0].reason
    assert "conanfile.py is executable" in run["s04_conanfile_py"][0].reason
    assert "has no tests" in run["s06_no_tests"][0].reason
    assert "baseline build/tests fail" in run["s07_baseline_broken"][0].reason


def test_crlf_is_preserved(run):
    wt = _wt(run["s08_crlf"][1])
    assert b"toydep::sum(2, 3) == 5 ? 0 : 1;\r\n" in (wt / "src" / "main.cpp").read_bytes()
    assert (wt / "conanfile.txt").read_bytes().count(b"\r\n") == (wt / "conanfile.txt").read_bytes().count(b"\n")


def test_non_code_mentions_are_left_alone(run):
    _o, state = run["s10_comments_and_strings"]
    assert state["changed_files"] == ["conanfile.txt"]
    assert "toydep::old_sum(1, 2) is deprecated" in (_wt(state) / "src" / "main.cpp").read_text()


def test_header_and_test_usages_are_migrated(run):
    _o, state = run["s05_header_and_tests"]
    assert sorted(state["changed_files"]) == ["conanfile.txt", "include/mylib/api.hpp", "tests/test_main.cpp"]
