"""Real Conan/CMake/CTest repair path: a usage hidden from impact analysis.

repo_d hides `old_sum` in src/calls.inc (not an indexed extension); repo_e builds it
by macro token-pasting. With the probe build disabled, repo_d is fixed by one
bounded repair and verified; repo_e cannot be repaired and fails honestly. With the
probe enabled, repo_d's hidden usage is caught before planning (no repair needed).
"""

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

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "toydep"


@pytest.fixture(scope="module")
def conan_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("conan-home")
    steps = prepare_local_packages([FIXTURES / "v1", FIXTURES / "v2"], home)
    assert all(s.succeeded for s in steps), [f"{s.label}: {s.output_tail}" for s in steps]
    return home


def _fleet(tmp_path, probe: bool) -> tuple[Fleet, dict[str, Path]]:
    sources = tmp_path / "sources"
    roots = {}
    for name in ("repo_d", "repo_e"):
        roots[name] = sources / name
        shutil.copytree(FIXTURES / "repositories" / name, roots[name])
    git = ["git", "-c", "user.name=f", "-c", "user.email=f@f"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=sources, check=True)
    subprocess.run([*git, "add", "-A"], cwd=sources, check=True)
    subprocess.run([*git, "commit", "-qm", "fixtures"], cwd=sources, check=True)
    policy = UpgradePolicy(allowed_change_levels=[ChangeLevel.MAJOR], require_human_approval_for_major=False,
                           run_probe_build=probe, max_repair_attempts=2)
    repos = [RepositoryRecord(repository_id=n, repo_url_or_path=str(p), display_name=n, allowed_paths=["src"])
             for n, p in roots.items()]
    return Fleet(FleetConfig(fleet_id="repair", repositories=repos, policies={"default": policy}), tmp_path), roots


def _run(tmp_path, probe):
    fleet, _ = _fleet(tmp_path, probe)
    event = ReleaseEvent(event_id=f"toydep-v2-probe-{probe}", dependency_name="toydep", upstream_repo="local/toydep",
                         previous_version="1.0.0", target_version="2.0.0",
                         changelog_path=str(FIXTURES / "release-notes-2.0.0.md"))
    summary = run_fleet(event, fleet, stages=load_default_stages(), state_dir=tmp_path / "state",
                        artifacts_root=tmp_path / "artifacts")
    states = {o.repository_id: json.loads((Path(o.report_path).parent / "state.json").read_text())
              for o in summary.outcomes}
    return {o.repository_id: o for o in summary.outcomes}, states


needs_tools = pytest.mark.skipif(shutil.which("conan") is None or shutil.which("cmake") is None,
                                 reason="Conan and CMake must be on PATH")


@needs_tools
def test_bounded_repair_fixes_hidden_usage_and_stops_honestly(tmp_path, conan_home, monkeypatch):
    monkeypatch.setenv(CONAN_HOME_ENV, str(conan_home))
    outcomes, states = _run(tmp_path, probe=False)
    d, e = states["repo_d"], states["repo_e"]
    assert outcomes["repo_d"].status.value == "verified" and d["repair_attempts"] == 1
    assert [h["to_status"] for h in d["history"]][-4:] == ["FAILED_BUILD", "PLANNED", "PATCHED", "VERIFIED"]
    (attempt,) = d["repair_attempt_details"]
    assert attempt["patches"][0]["paths"] == ["src/calls.inc"] and attempt["patches"][0]["status"] == "applied"
    assert sorted(d["changed_files"]) == ["conanfile.txt", "src/calls.inc", "src/main.cpp"]
    assert d["verification"]["build"]["exit_code"] == 0 and d["verification"]["tests"]["exit_code"] == 0
    assert outcomes["repo_e"].status.value == "failed" and e["status"] == "FAILED_BUILD"
    assert "no repair proposed" in e["status_reason"] and e["repair_attempts"] == 0


@needs_tools
def test_probe_catches_hidden_usage_before_planning(tmp_path, conan_home, monkeypatch):
    monkeypatch.setenv(CONAN_HOME_ENV, str(conan_home))
    outcomes, states = _run(tmp_path, probe=True)
    d = states["repo_d"]
    assert outcomes["repo_d"].status.value == "verified" and d["repair_attempts"] == 0
    inc = [u for u in d["usages"] if u["file_path"] == "src/calls.inc"]
    assert inc and inc[0]["impact"] == "definite" and inc[0]["resolved_by"] == "compiler_probe"
    assert outcomes["repo_e"].status.value == "failed"
