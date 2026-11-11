"""Run benchmark cases through the real migration engine.

Each case gets a pristine copy of its pinned repository (content hash or commit
verified first), an isolated Conan home with the case's recipes, and one
``RepositoryJobRunner`` job with the default stages — the same code path as
``depbot upgrade``. Nothing is mocked; a case whose requirements are missing is
reported ``not_run`` with the reason.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from depbot import __version__
from depbot.benchmark.evaluator import aggregate, evaluate_case
from depbot.benchmark.models import BenchmarkCase, BenchmarkRegistry, BenchmarkResults, CaseResult
from depbot.benchmark.report import render_markdown
from depbot.clang_support import ClangUnavailable, load_cindex
from depbot.execution import ExecutionBackend, ExecutionContext
from depbot.execution.local import LocalBackend
from depbot.fleet.dependency_index import scan_repository
from depbot.fleet.versions import UnparseableVersion, compare
from depbot.interfaces import PipelineStages
from depbot.llm.runtime import LLMFactory
from depbot.models import (
    ChangeLevel,
    FleetClassification,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryRecord,
    RepositoryUpgradeJob,
    RepositoryUpgradeOutcome,
    UpgradePolicy,
    utc_now_iso,
)
from depbot.orchestrator import RepositoryJobRunner
from depbot.package_managers.conan import CONAN_HOME_ENV
from depbot.package_managers.conan_setup import SetupError, prepare_local_packages
from depbot.telemetry import host_toolchain

log = logging.getLogger(__name__)

_IGNORED_DIRS = {".git", "build", ".depbot", ".work", "vcpkg_installed", "__pycache__"}
_IGNORED_FILES = {"CMakeUserPresets.json", ".DS_Store"}
_GIT = ["git", "-c", "user.name=depbot-bench", "-c", "user.email=bench@depbot.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


class CaseSkipped(Exception):
    """The case cannot run here; the message says why."""


def tree_sha256(root: Path) -> str:
    """Content hash of a fixture repository: relative paths and file bytes, sorted."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if _IGNORED_DIRS & set(rel.parts) or rel.name in _IGNORED_FILES:
            continue
        digest.update(rel.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _network_available() -> bool:
    return os.environ.get("DEPBOT_BENCH_NETWORK") == "1"


def missing_requirements(case: BenchmarkCase) -> list[str]:
    missing = [tool for tool in ("conan", "cmake", "git") if tool in case.requires and shutil.which(tool) is None]
    if "network" in case.requires and not _network_available():
        missing.append("network (set DEPBOT_BENCH_NETWORK=1 to allow it)")
    if "libclang" in case.requires:
        try:
            load_cindex()
        except ClangUnavailable:
            missing.append("libclang")
    return missing


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run([*_GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=300,
                          shell=False, stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise CaseSkipped(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def materialize_repository(case: BenchmarkCase, registry_dir: Path, dest: Path) -> str:
    """A pristine Git checkout of the case repository at ``dest``; returns its commit."""
    spec = case.repository
    if spec.url is not None:
        _git(dest.parent, "clone", "-q", "--", spec.url, str(dest))
        _git(dest, "checkout", "-q", "--detach", spec.commit or "")
        head = _git(dest, "rev-parse", "HEAD")
        if not head.startswith(spec.commit or "") and head != _git(dest, "rev-parse", f"{spec.commit}^{{commit}}"):
            raise CaseSkipped(f"checked out {head}, not the pinned commit {spec.commit}")
        return head
    source = (registry_dir / (spec.path or "")).resolve()
    if not source.is_dir():
        raise CaseSkipped(f"repository fixture not found: {source}")
    actual = tree_sha256(source)
    if actual != spec.tree_sha256:
        raise CaseSkipped(f"repository fixture drifted: tree_sha256 is {actual}, registry pins {spec.tree_sha256}")
    shutil.copytree(source, dest, ignore=lambda _d, names: [n for n in names
                                                            if n in _IGNORED_DIRS or n in _IGNORED_FILES])
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"benchmark fixture {case.id}")
    return _git(dest, "rev-parse", "HEAD")


class BenchmarkRunner:
    def __init__(self, registry: BenchmarkRegistry, registry_path: Path, out_dir: Path, *,
                 stages_factory: Callable[[], PipelineStages], llm_factory: LLMFactory | None = None,
                 llm_label: str = "none", backend: ExecutionBackend | None = None) -> None:
        self.registry, self.registry_dir = registry, Path(registry_path).resolve().parent
        self.out_dir = Path(out_dir).resolve()
        self.stages_factory, self.llm_factory, self.llm_label = stages_factory, llm_factory, llm_label
        self.backend = backend or LocalBackend()
        self.conan_home = self.out_dir / "conan-home"
        self._prepared: set[tuple[str, ...]] = set()

    # -- setup ---------------------------------------------------------------------

    def _prepare_packages(self, case: BenchmarkCase) -> None:
        recipes = tuple(str((self.registry_dir / r).resolve()) for r in case.setup.conan_recipes)
        if not recipes or recipes in self._prepared:
            return
        try:
            steps = prepare_local_packages([Path(r) for r in recipes], self.conan_home)
        except SetupError as exc:
            raise CaseSkipped(f"package setup failed: {exc}") from exc
        failed = [s for s in steps if not s.succeeded]
        if failed:
            raise CaseSkipped(f"package setup step {failed[0].label} failed: {failed[0].output_tail[-300:]}")
        self._prepared.add(recipes)

    def _job(self, case: BenchmarkCase, root: Path) -> RepositoryUpgradeJob | RepositoryUpgradeOutcome:
        repo = RepositoryRecord(repository_id=case.id, repo_url_or_path=str(root), display_name=case.id,
                                default_branch="main")
        scan = scan_repository(repo, root)
        declared = [d for d in scan.dependencies if d.dependency_name == case.dependency]
        if scan.error or len(declared) != 1:
            # The same refusal `depbot upgrade` gives: nothing can be upgraded safely.
            return RepositoryUpgradeOutcome(
                repository_id=case.id, status=OutcomeStatus.BLOCKED,
                classification=FleetClassification.NEEDS_RESCAN,
                reason=scan.error or f"{case.dependency} declared {len(declared)} times",
                dependency_name=case.dependency, to_version=case.target_version,
            )
        record = declared[0]
        try:
            if compare(record.current_version, case.target_version) >= 0:
                return RepositoryUpgradeOutcome(
                    repository_id=case.id, status=OutcomeStatus.SKIPPED,
                    classification=FleetClassification.ALREADY_AT_TARGET_OR_NEWER,
                    reason=f"already on {case.dependency} {record.current_version}",
                    dependency_name=case.dependency, from_version=record.current_version,
                    to_version=case.target_version,
                )
        except UnparseableVersion as exc:
            return RepositoryUpgradeOutcome(
                repository_id=case.id, status=OutcomeStatus.BLOCKED,
                classification=FleetClassification.NEEDS_RESCAN, reason=str(exc),
                dependency_name=case.dependency, to_version=case.target_version,
            )
        changelog = (self.registry_dir / case.changelog).resolve() if case.changelog else None
        event = ReleaseEvent(
            event_id=f"bench-{case.id}", dependency_name=case.dependency, upstream_repo=case.upstream_repo,
            previous_version=case.baseline_version, target_version=case.target_version,
            changelog_path=str(changelog) if changelog else None,
        )
        policy = UpgradePolicy(
            policy_id="benchmark",
            allowed_change_levels=[ChangeLevel.PATCH, ChangeLevel.MINOR, ChangeLevel.MAJOR],
            major_approved=True, require_tests=case.verification.require_tests,
            build_configurations=case.verification.build_configurations,
        )
        return RepositoryUpgradeJob(job_id=event.event_id, event=event, repository=repo,
                                    dependency_record=record, policy=policy, repo_root=str(root))

    # -- execution -----------------------------------------------------------------

    def run_case(self, case: BenchmarkCase) -> CaseResult:
        blank = dict(case_id=case.id, split=case.split, expected_outcome=case.expected.outcome.value)
        missing = missing_requirements(case)
        if missing:
            return CaseResult(status="not_run", reason="missing requirement: " + ", ".join(missing), **blank)
        work = self.out_dir / "cases" / case.id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        previous_home = os.environ.get(CONAN_HOME_ENV)
        try:
            materialize_repository(case, self.registry_dir, work / "repo")
            self._prepare_packages(case)
            os.environ[CONAN_HOME_ENV] = str(self.conan_home)
            job = self._job(case, work / "repo")
            if isinstance(job, RepositoryUpgradeOutcome):
                return evaluate_case(case, job, None, None)
            runner = RepositoryJobRunner(job, self.stages_factory(), work / "runs", self.llm_factory,
                                         execution=ExecutionContext(backend=self.backend))
            state, outcome = runner.run()
            return evaluate_case(case, outcome, state, runner.telemetry)
        except CaseSkipped as exc:
            return CaseResult(status="not_run", reason=str(exc), **blank)
        except Exception as exc:  # a harness failure is an error, never a scored result
            log.exception("benchmark case %s crashed", case.id)
            return CaseResult(status="error", reason=f"{type(exc).__name__}: {exc}", **blank)
        finally:
            if previous_home is None:
                os.environ.pop(CONAN_HOME_ENV, None)
            else:
                os.environ[CONAN_HOME_ENV] = previous_home

    def run(self, *, splits: list[str], case_ids: list[str] | None = None,
            on_case: Callable[[CaseResult], None] | None = None) -> BenchmarkResults:
        selected = [c for c in self.registry.cases
                    if c.split in splits and (not case_ids or c.id in case_ids)]
        self.out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for case in selected:
            result = self.run_case(case)
            results.append(result)
            if on_case is not None:
                on_case(result)
        groups = {"overall": results, **{s: [r for r in results if r.split == s] for s in splits}}
        report = BenchmarkResults(
            suite=self.registry.suite, suite_version=self.registry.suite_version,
            generated_at=utc_now_iso(), depbot_version=__version__, depbot_commit=_depbot_commit(),
            llm=self.llm_label, splits_run=splits, toolchain=host_toolchain(),
            execution=self.backend.describe(),
            aggregate={name: aggregate(rows) for name, rows in groups.items()},
            cases=results,
        )
        write_results(report, self.out_dir)
        return report


def _depbot_commit() -> str | None:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def write_results(results: BenchmarkResults, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "results.json", out_dir / "report.md"
    json_path.write_text(results.model_dump_json(indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(results), encoding="utf-8")
    return json_path, md_path


def default_out_dir(base: Path) -> Path:
    return base / datetime.now(timezone.utc).strftime("bench-%Y%m%dT%H%M%SZ")
