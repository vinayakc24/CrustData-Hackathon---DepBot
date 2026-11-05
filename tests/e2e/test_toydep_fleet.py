"""Three-repository acceptance run using real Conan, CMake, and CTest commands."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from depbot.fleet.registry import Fleet, FleetConfig
from depbot.fleet.runner import run_fleet
from depbot.models import (
    AnalysisMethod,
    BreakingChange,
    ChangeLevel,
    ChangeType,
    CodeUsage,
    ImpactLevel,
    ReleaseEvent,
    RepositoryRecord,
    UpgradePolicy,
)
from depbot.orchestrator import load_default_stages
from depbot.package_managers.conan import CONAN_HOME_ENV
from depbot.package_managers.conan_setup import prepare_local_packages


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "toydep"


def _research(*, event, dependency, artifacts_dir):
    release_notes = FIXTURE_ROOT / "release-notes-2.0.0.md"
    return [BreakingChange(
        id="toydep-2.0.0-BC1",
        title="old_sum removed and replaced by sum",
        change_type=ChangeType.REMOVED,
        affected_symbols=["old_sum"],
        replacement_symbols=["sum"],
        migration_advice="Use sum; addition behavior is unchanged.",
        evidence_urls=[release_notes.as_uri()],
        evidence_excerpt="removes toydep::old_sum; replace with toydep::sum",
        confidence=0.99,
    )]


@pytest.mark.skipif(
    shutil.which("conan") is None or shutil.which("cmake") is None,
    reason="Conan and CMake must be on PATH",
)
def test_release_migrates_two_repositories_and_skips_current_one(tmp_path: Path, monkeypatch):
    conan_home = tmp_path / "conan-home"
    recipes = [FIXTURE_ROOT / "v1", FIXTURE_ROOT / "v2"]
    setup_steps = prepare_local_packages(recipes, conan_home)
    assert len(setup_steps) == len(recipes) + 2
    assert all(step.succeeded for step in setup_steps), [
        f"{step.label}: {step.exit_code}; {step.output_tail}" for step in setup_steps
    ]
    monkeypatch.setenv(CONAN_HOME_ENV, str(conan_home))

    source_roots = {}
    for repo_name in ("repo_a", "repo_b", "repo_c"):
        root = tmp_path / "sources" / repo_name
        shutil.copytree(FIXTURE_ROOT / "repositories" / repo_name, root)
        source_roots[repo_name] = root

    # Use a clean parent Git root so the production working-copy path is exercised.
    source_repo = tmp_path / "sources"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source_repo, check=True, shell=False)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "add", "-A"],
        cwd=source_repo, check=True, shell=False,
    )
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
         "commit", "-qm", "fixture sources"],
        cwd=source_repo, check=True, shell=False,
    )

    repositories = [
        RepositoryRecord(
            repository_id=name,
            repo_url_or_path=str(path),
            display_name=name.upper(),
            allowed_paths=["src"],
        )
        for name, path in source_roots.items()
    ]
    policy = UpgradePolicy(
        allowed_change_levels=[ChangeLevel.MAJOR],
        require_human_approval_for_major=False,
        require_baseline=True,
        require_tests=True,
        minimum_migration_confidence=0.80,
    )
    fleet = Fleet(
        FleetConfig(fleet_id="toydep-fleet", repositories=repositories, policies={"default": policy}),
        tmp_path,
    )
    event = ReleaseEvent(
        event_id="toydep-release-2.0.0",
        dependency_name="toydep",
        upstream_repo="local/toydep",
        previous_version="1.0.0",
        target_version="2.0.0",
        source_url=(FIXTURE_ROOT / "release-notes-2.0.0.md").as_uri(),
    )

    research_versions: list[str] = []

    def research(*, event, dependency, artifacts_dir):
        research_versions.append(dependency.current_version)
        return _research(event=event, dependency=dependency, artifacts_dir=artifacts_dir)

    stages = replace(load_default_stages(), research=research)
    summary = run_fleet(
        event,
        fleet,
        stages=stages,
        state_dir=tmp_path / "state",
        artifacts_root=tmp_path / "artifacts",
    )

    assert (
        summary.repositories_considered,
        summary.repositories_affected,
        summary.repositories_skipped,
        summary.repositories_verified,
        summary.repositories_failed_or_blocked,
    ) == (3, 2, 1, 2, 0)
    by_id = {outcome.repository_id: outcome for outcome in summary.outcomes}
    assert by_id["repo_a"].status.value == by_id["repo_b"].status.value == "verified"
    assert by_id["repo_a"].verification_passed and by_id["repo_b"].verification_passed
    assert by_id["repo_c"].status.value == "skipped"
    assert "already on toydep 2.0.0" in by_id["repo_c"].reason
    assert by_id["repo_c"].run_id is None
    assert research_versions == ["1.0.0", "1.0.0"]

    expected_commands = {
        "dependency_install": "conan",
        "configure": "cmake",
        "build": "cmake",
        "tests": "ctest",
    }
    for name in ("repo_a", "repo_b"):
        report_path = Path(by_id[name].report_path)
        assert report_path.is_file()
        assert report_path.name == "upgrade-report.md"
        report = report_path.read_text(encoding="utf-8")
        assert "Upgraded verification passed: **True**" in report
        assert "1 passed, 0 failed" in report
        state = json.loads((report_path.parent / "state.json").read_text(encoding="utf-8"))
        commands = {result["command_label"]: result for result in state["command_results"]}
        for phase in ("baseline", "probe", "upgraded"):
            for step, executable in expected_commands.items():
                result = commands[f"{phase}.{step}"]
                assert result["exit_code"] == 0, result
                assert not result["timed_out"], result
                assert Path(result["args"][0]).name.lower().startswith(executable), result
    # repo_c is selected as already current before research or verification starts.
    assert "2.0.0" not in research_versions
