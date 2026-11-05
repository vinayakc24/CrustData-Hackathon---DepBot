"""Offline three-repository acceptance path through Member A's real fleet engine."""

import shutil
import subprocess
import os
from pathlib import Path

import pytest

from depbot.fleet.registry import Fleet, FleetConfig
from depbot.fleet.runner import run_fleet
from depbot.interfaces import PipelineStages
from depbot.migration.patch_generator import generate_patches
from depbot.migration.patch_validator import validate_and_apply
from depbot.migration.planner import plan_migration
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
from depbot.package_managers.conan import ConanAdapter
from depbot.reporting.report_generator import write_repository_report
from depbot.verification.pipeline import run_verification


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "toydep"
REPO_ROOT = Path(__file__).resolve().parents[2]


class _FixtureAdapter(ConanAdapter):
    def command_env(self):
        if shutil.which("mingw32-make"):
            return {"CMAKE_GENERATOR": "MinGW Makefiles"}
        if os.name != "nt":
            return {"CMAKE_GENERATOR": "Unix Makefiles"}
        return {}

    def install_args(self, repo_root: Path, *, build_type="Release"):
        version = self.find_dependency(repo_root, "toydep").version
        source = FIXTURE_ROOT / ("v1" if version == "1.0.0" else "v2")
        return ["cmake", "-E", "copy_directory", str(source), str(repo_root / "build" / "dependency" / "toydep")]


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


def _impact(*, worktree, changes, artifacts_dir):
    usages = []
    for source in sorted(worktree.glob("src/**/*.cpp")):
        for line_number, text in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if "old_sum" in text:
                usages.append(CodeUsage(
                    symbol="old_sum",
                    file_path=source.relative_to(worktree).as_posix(),
                    line_start=line_number,
                    line_end=line_number,
                    snippet=text.strip(),
                    analysis_method=AnalysisMethod.RIPGREP,
                    impact=ImpactLevel.DEFINITE,
                    related_change_ids=[changes[0].id],
                    confidence=0.99,
                ))
    return usages


@pytest.mark.skipif(shutil.which("cmake") is None, reason="CMake is not installed")
def test_release_migrates_two_repositories_and_skips_current_one(tmp_path: Path, monkeypatch):
    source_roots = {}
    for repo_name in ("repo_a", "repo_b", "repo_c"):
        root = tmp_path / "sources" / repo_name
        shutil.copytree(FIXTURE_ROOT / "repositories" / repo_name, root)
        (root / "vendor").mkdir(exist_ok=True)
        shutil.copytree(FIXTURE_ROOT / ("v1" if repo_name != "repo_c" else "v2"), root / "vendor" / "toydep")
        source_roots[repo_name] = root
    # Keep the registered repositories clean and nested under this fixture repo.
    # The production worktree helper will then take its isolated snapshot path,
    # avoiding a clone and preserving the original fixture tree.
    source_repo = tmp_path / "sources"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source_repo, check=True, shell=False)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "add", "-A"],
        cwd=source_repo, check=True, shell=False,
    )
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture sources"],
        cwd=source_repo, check=True, shell=False,
    )

    # The test substitutes only the package download with a local deterministic
    # CMake fixture. Manifest parsing/update, worktrees, patches, builds, tests,
    # selection, job outcomes and summary are the production implementations.
    import depbot.fleet.dependency_index as index_module
    import depbot.orchestrator as orchestrator_module

    monkeypatch.setattr(index_module, "detect_adapter", lambda _root: _FixtureAdapter())
    monkeypatch.setattr(orchestrator_module, "detect_adapter", lambda _root: _FixtureAdapter())

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
        FleetConfig(
            fleet_id="toydep-fleet",
            repositories=repositories,
            policies={"default": policy},
        ),
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
    stages = PipelineStages(
        research=_research,
        impact=_impact,
        plan=plan_migration,
        patch=generate_patches,
        apply=validate_and_apply,
        verify=run_verification,
        report=write_repository_report,
    )
    summary = run_fleet(
        event,
        fleet,
        stages=stages,
        state_dir=tmp_path / "state",
        artifacts_root=REPO_ROOT / "artifacts" / "member-c-e2e",
    )

    assert (summary.repositories_considered, summary.repositories_affected,
            summary.repositories_skipped, summary.repositories_verified,
            summary.repositories_failed_or_blocked) == (3, 2, 1, 2, 0)
    by_id = {outcome.repository_id: outcome for outcome in summary.outcomes}
    assert by_id["repo_a"].verification_passed and by_id["repo_b"].verification_passed
    assert by_id["repo_c"].status.value == "skipped"
    assert "already on toydep 2.0.0" in by_id["repo_c"].reason
    for name in ("repo_a", "repo_b"):
        report = Path(by_id[name].report_path)
        assert report.is_file()
        assert "Upgraded verification passed: **True**" in report.read_text(encoding="utf-8")
        assert "old_sum" in (source_roots[name] / "src" / "main.cpp").read_text(encoding="utf-8")
    assert "toydep::product" in (source_roots["repo_c"] / "src" / "main.cpp").read_text(encoding="utf-8")
