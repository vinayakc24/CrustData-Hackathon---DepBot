"""Real CMake and CTest interaction for migration -> apply -> verification."""

import shutil
import subprocess
import os
from pathlib import Path

import pytest

from depbot.migration.patch_generator import generate_patches
from depbot.migration.patch_validator import validate_and_apply
from depbot.migration.planner import plan_migration
from depbot.models import (
    AnalysisMethod,
    BreakingChange,
    ChangeType,
    CodeUsage,
    Dependency,
    ImpactLevel,
    PackageManagerName,
    UpgradePolicy,
    VerificationPhase,
)
from depbot.package_managers.conan import ConanAdapter
from depbot.verification.pipeline import run_verification


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "toydep"


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


def _initialize_git(path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, shell=False)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "add", "-A"], cwd=path, check=True, shell=False)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture baseline"], cwd=path, check=True, shell=False)


@pytest.mark.skipif(shutil.which("cmake") is None, reason="CMake is not installed")
def test_two_repositories_migrate_and_pass_real_cmake_ctest(tmp_path: Path):
    for repo_name in ("repo_a", "repo_b"):
        worktree = tmp_path / repo_name
        shutil.copytree(FIXTURE_ROOT / "repositories" / repo_name, worktree)
        (worktree / "vendor").mkdir(exist_ok=True)
        shutil.copytree(FIXTURE_ROOT / "v1", worktree / "vendor" / "toydep")
        _initialize_git(worktree)
        adapter = _FixtureAdapter()
        baseline = run_verification(
            worktree=worktree,
            adapter=adapter,
            phase=VerificationPhase.BASELINE,
            policy=UpgradePolicy(require_tests=True),
            artifacts_dir=tmp_path / "artifacts" / repo_name,
        )
        assert baseline.passed, baseline.model_dump_json(indent=2)

        change = BreakingChange(
            id="toydep-2.0.0-BC1",
            title="old_sum removed",
            change_type=ChangeType.REMOVED,
            affected_symbols=["old_sum"],
            replacement_symbols=["sum"],
            migration_advice="Replace old_sum with sum.",
            evidence_urls=[(FIXTURE_ROOT / "release-notes-2.0.0.md").as_uri()],
            confidence=0.99,
        )
        source = worktree / "src" / "main.cpp"
        line = next(i for i, text in enumerate(source.read_text(encoding="utf-8").splitlines(), 1) if "old_sum" in text)
        snippet = source.read_text(encoding="utf-8").splitlines()[line - 1].strip()
        usage = CodeUsage(
            symbol="old_sum", file_path="src/main.cpp", line_start=line, line_end=line,
            snippet=snippet, analysis_method=AnalysisMethod.RIPGREP,
            impact=ImpactLevel.DEFINITE, related_change_ids=[change.id], confidence=0.99,
        )
        dependency = Dependency(
            name="toydep", current_version="1.0.0", target_version="2.0.0",
            package_manager=PackageManagerName.CONAN, manifest_path="conanfile.txt",
        )
        plan = plan_migration(dependency=dependency, changes=[change], usages=[usage])
        patches = generate_patches(worktree=worktree, plan=plan, changes=[change], usages=[usage])
        assert len(patches) == 1
        applied = validate_and_apply(worktree=worktree, patch=patches[0], allowed_paths=["src"])
        assert applied.status.value == "applied", applied.rejection_reason

        adapter.update_dependency(worktree, "toydep", "2.0.0")
        upgraded = run_verification(
            worktree=worktree,
            adapter=adapter,
            phase=VerificationPhase.UPGRADED,
            policy=UpgradePolicy(require_tests=True),
            artifacts_dir=tmp_path / "artifacts" / repo_name,
        )
        assert upgraded.passed, upgraded.model_dump_json(indent=2)
        assert upgraded.tests_passed == 1
        assert "old_sum" not in source.read_text(encoding="utf-8")

        # The source templates remain intact; each repo is migrated in its own copy.
        assert "old_sum" in (FIXTURE_ROOT / "repositories" / repo_name / "src" / "main.cpp").read_text(encoding="utf-8")
