"""Repository migration engine: runs ONE RepositoryUpgradeJob end to end.

Owner: Member A. Deterministic Python owns every transition; stages (B, C) are
injected callables that return typed values. Nothing here decides *which*
repositories run — that is ``depbot.fleet.runner``.

    INITIALIZED → INSPECTED (working copy, adapter, baseline) → RESEARCHED
    → IMPACT_ANALYZED → PLANNED (+ policy gate) → PATCHED (manifest + source)
    → VERIFIED | NO_CHANGES_NEEDED | FAILED_* | BLOCKED
"""

from __future__ import annotations

import importlib
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from depbot.interfaces import PipelineStages, StageError
from depbot.models import (
    Dependency,
    FleetClassification,
    OutcomeStatus,
    PatchStatus,
    RepositoryUpgradeJob,
    RepositoryUpgradeOutcome,
    VerificationPhase,
    VerificationResult,
)
from depbot.package_managers.base import PackageManagerAdapter, PackageManagerError
from depbot.package_managers.detector import detect_adapter
from depbot.fleet.versions import UnparseableVersion, compare
from depbot.policy.engine import plan_confidence_gate
from depbot.repository.paths import UnsafePath, is_allowed, safe_join
from depbot.repository.worktree import (
    UnsafeRepository,
    branch_name,
    changed_files,
    commit_all,
    full_diff,
    prepare_working_copy,
)
from depbot.state import FAILURE_STATUSES, DepBotState, JobStatus

log = logging.getLogger(__name__)


class _SkipJob(Exception):
    """Revalidation found the repository no longer needs this upgrade."""


# ------------------------------------------------------------------ default stages

_STAGE_TARGETS = {
    "research": "depbot.research.breaking_change_analyzer:research_release",
    "impact": "depbot.code_intelligence.impact_analyzer:find_impacted_usages",
    "plan": "depbot.migration.planner:plan_migration",
    "patch": "depbot.migration.patch_generator:generate_patches",
    "apply": "depbot.migration.patch_validator:validate_and_apply",
    "verify": "depbot.verification.pipeline:run_verification",
    "report": "depbot.reporting.report_generator:write_repository_report",
}


def _not_integrated(stage: str, target: str):
    def _stage(**_kwargs):
        raise StageError(f"stage '{stage}' not integrated yet (expected {target})", blocked=True)

    return _stage


def load_default_stages() -> PipelineStages:
    """Resolve B's and C's stage functions; missing ones block jobs honestly."""
    resolved = {}
    for stage, target in _STAGE_TARGETS.items():
        module_name, func_name = target.split(":")
        try:
            resolved[stage] = getattr(importlib.import_module(module_name), func_name)
        except (ImportError, AttributeError):
            resolved[stage] = _not_integrated(stage, target.replace(":", "."))
    return PipelineStages(**resolved)


# ------------------------------------------------------------------------- helpers


def new_run_id(job: RepositoryUpgradeJob) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{job.event.event_id}-{job.repository.repository_id}-{stamp}-{secrets.token_hex(3)}"


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _verification_failure(result: VerificationResult, require_tests: bool) -> tuple[JobStatus, str] | None:
    """Map an upgraded verification result to a failure status, or None if it passed."""
    steps = [
        (result.dependency_install, JobStatus.FAILED_INSTALL, "dependency install"),
        (result.configure, JobStatus.FAILED_BUILD, "CMake configure"),
        (result.build, JobStatus.FAILED_BUILD, "build"),
    ]
    for step, status, label in steps:
        if step is None:
            return status, f"{label} did not run"
        if not step.succeeded:
            why = "timed out" if step.timed_out else f"exit code {step.exit_code}"
            return status, f"{label} failed ({why})"
    if result.tests is None:
        if require_tests:
            return JobStatus.FAILED_TESTS, "policy requires tests but no test command ran"
    elif not result.tests.succeeded:
        failed = f"{result.tests_failed} failed" if result.tests_failed is not None else "failed"
        return JobStatus.FAILED_TESTS, f"tests {failed} (exit code {result.tests.exit_code})"
    if not result.passed:
        return JobStatus.FAILED_TESTS, "verification stage reported passed=False"
    return None


def _outcome_status(status: JobStatus) -> OutcomeStatus:
    if status in (JobStatus.VERIFIED, JobStatus.PR_CREATED):
        return OutcomeStatus.VERIFIED
    if status == JobStatus.NO_CHANGES_NEEDED:
        return OutcomeStatus.UNAFFECTED
    if status == JobStatus.BLOCKED:
        return OutcomeStatus.BLOCKED
    return OutcomeStatus.FAILED


# ---------------------------------------------------------------------------- engine


class RepositoryJobRunner:
    def __init__(self, job: RepositoryUpgradeJob, stages: PipelineStages, artifacts_root: Path) -> None:
        self.job = job
        self.stages = stages
        self.run_id = new_run_id(job)
        self.artifacts_dir = (artifacts_root / self.run_id).resolve()
        record = job.dependency_record
        self.state = DepBotState(
            run_id=self.run_id,
            repository_id=job.repository.repository_id,
            original_repo=job.repo_root,
            worktree_path=str(self.artifacts_dir / "worktree"),
            base_branch="depbot-baseline",
            working_branch=branch_name(job.event.dependency_name, job.event.target_version),
            dependency=Dependency(
                name=job.event.dependency_name,
                current_version=record.current_version,
                target_version=job.event.target_version,
                package_manager=record.package_manager,
                manifest_path=record.manifest_path,
                is_direct=record.is_direct,
            ),
            artifacts_dir=str(self.artifacts_dir),
            upstream_repo=job.event.upstream_repo,
            max_repair_attempts=job.policy.max_repair_attempts,
        )
        self.adapter: PackageManagerAdapter | None = None
        self.policy_decisions: list[dict] = []

    @property
    def worktree(self) -> Path:
        return Path(self.state.worktree_path)

    # -- public ------------------------------------------------------------------

    def run(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        skipped_reason: str | None = None
        try:
            self._inspect()
            self._research()
            self._impact()
            self._plan()
            self._patch()
            self._verify()
        except _SkipJob as exc:
            skipped_reason = str(exc)
            self._fail(JobStatus.BLOCKED, skipped_reason)
        except StageError as exc:
            self._fail(JobStatus.BLOCKED if exc.blocked else self._stage_failure(), str(exc))
        except UnsafePath as exc:
            self._fail(JobStatus.BLOCKED, f"unsafe path from stage output: {exc}")
        except Exception as exc:  # a bug in a stage or in DepBot: fail this job only
            log.exception("job %s crashed", self.run_id)
            self._fail(self._stage_failure(), f"internal error: {type(exc).__name__}: {exc}")
        self._finalize()
        return self.state, self._outcome(skipped_reason)

    # -- stages ------------------------------------------------------------------

    def _inspect(self) -> None:
        policy = self.job.policy
        try:
            copy = prepare_working_copy(
                Path(self.job.repo_root),
                self.worktree,
                branch=self.state.working_branch,
                refuse_dirty=policy.refuse_dirty_worktree,
            )
        except UnsafeRepository as exc:
            raise StageError(str(exc), blocked=True) from exc
        facts: dict[str, object] = {"working_copy": copy.model_dump(mode="json")}
        if not (self.worktree / "CMakeLists.txt").is_file():
            raise StageError("no top-level CMakeLists.txt; only CMake projects are supported", blocked=True)
        try:
            self.adapter = detect_adapter(self.worktree)
            declared = self.adapter.find_dependency(self.worktree, self.state.dependency.name)
        except PackageManagerError as exc:
            raise StageError(f"package manager: {exc}", blocked=True) from exc
        # The index is only a candidate filter: revalidate against the fresh copy.
        try:
            if compare(declared.version, self.state.dependency.target_version) >= 0:
                raise _SkipJob(
                    f"revalidation: already on {declared.name} {declared.version}; nothing to do"
                )
        except UnparseableVersion as exc:
            raise StageError(str(exc), blocked=True) from exc
        if declared.version != self.state.dependency.current_version:
            self.state.dependency = self.state.dependency.model_copy(
                update={"current_version": declared.version}
            )
            facts["inventory_was_stale"] = True
        facts.update(
            package_manager=self.adapter.name.value,
            manifest=declared.manifest_path,
            declared=declared.raw,
            cmake_preset=self.adapter.cmake_preset(),
        )
        self.state.repo_facts = facts
        _write(self.artifacts_dir / "inspection.json", json.dumps(facts, indent=2))

        if policy.require_baseline:
            baseline = self.stages.verify(
                worktree=self.worktree,
                adapter=self.adapter,
                phase=VerificationPhase.BASELINE,
                policy=policy,
                artifacts_dir=self.artifacts_dir,
            )
            self._record_commands(baseline)
            self.state.baseline = baseline
            if not baseline.passed:
                raise StageError(
                    "baseline build/tests fail before any change; refusing to migrate "
                    "(cannot separate regressions from pre-existing failures)",
                    blocked=True,
                )
        self.state.transition(JobStatus.INSPECTED)

    def _research(self) -> None:
        changes = self.stages.research(
            event=self.job.event, dependency=self.state.dependency, artifacts_dir=self.artifacts_dir
        )
        self.state.breaking_changes = changes
        _write(
            self.artifacts_dir / "breaking_changes.json",
            json.dumps([c.model_dump(mode="json") for c in changes], indent=2),
        )
        self.state.transition(JobStatus.RESEARCHED, f"{len(changes)} breaking change(s)")

    def _impact(self) -> None:
        usages = self.stages.impact(
            worktree=self.worktree, changes=self.state.breaking_changes, artifacts_dir=self.artifacts_dir
        )
        known = {c.id for c in self.state.breaking_changes}
        for usage in usages:
            safe_join(self.worktree, usage.file_path)
            unknown = set(usage.related_change_ids) - known
            if unknown:
                raise StageError(f"usage {usage.file_path}:{usage.line_start} cites unknown change {sorted(unknown)}")
        self.state.usages = usages
        _write(
            self.artifacts_dir / "impact.json",
            json.dumps([u.model_dump(mode="json") for u in usages], indent=2),
        )
        self.state.transition(JobStatus.IMPACT_ANALYZED, f"{len(usages)} usage(s)")

    def _plan(self) -> None:
        plan = self.stages.plan(
            dependency=self.state.dependency,
            changes=self.state.breaking_changes,
            usages=self.state.usages,
        )
        known = {c.id for c in self.state.breaking_changes}
        for item in plan:
            safe_join(self.worktree, item.file_path)
            unknown = set(item.related_change_ids) - known
            if unknown:
                raise StageError(f"plan item {item.file_path} cites unknown change {sorted(unknown)}")
        self.state.migration_plan = plan
        _write(
            self.artifacts_dir / "plan.json",
            json.dumps([p.model_dump(mode="json") for p in plan], indent=2),
        )
        self.state.transition(JobStatus.PLANNED, f"{len(plan)} planned edit(s)")
        decision = plan_confidence_gate(self.job.policy, self.state.breaking_changes, plan)
        self.policy_decisions.append(decision.model_dump(mode="json"))
        if not decision.allowed:
            raise StageError("policy: " + "; ".join(decision.reasons), blocked=True)

    def _allowed_paths(self) -> list[str]:
        # Explicit registry scope, else exactly the files the plan names.
        return self.job.repository.allowed_paths or sorted({p.file_path for p in self.state.migration_plan})

    def _patch(self) -> None:
        assert self.adapter is not None
        try:
            change = self.adapter.update_dependency(
                self.worktree, self.state.dependency.name, self.state.dependency.target_version
            )
        except PackageManagerError as exc:
            raise StageError(f"manifest update refused: {exc}", blocked=True) from exc
        self.state.manifest_change = change
        _write(self.artifacts_dir / "patches" / "manifest.diff", change.unified_diff)

        allowed = self._allowed_paths()
        if self.state.migration_plan:
            patches = self.stages.patch(
                worktree=self.worktree,
                plan=self.state.migration_plan,
                changes=self.state.breaking_changes,
                usages=self.state.usages,
            )
            for patch in patches:
                for rel in patch.files:
                    try:
                        safe_join(self.worktree, rel)
                    except UnsafePath as exc:
                        raise StageError(f"patch {patch.patch_id}: {exc}", blocked=True) from exc
                    if not is_allowed(rel, allowed):
                        raise StageError(
                            f"patch {patch.patch_id} touches {rel}, outside approved scope {allowed}",
                            blocked=True,
                        )
                applied = self.stages.apply(worktree=self.worktree, patch=patch, allowed_paths=allowed)
                self.state.patches.append(applied)
                if applied.status != PatchStatus.APPLIED:
                    raise StageError(
                        f"patch {patch.patch_id} not applied: {applied.rejection_reason or applied.status}"
                    )
        # Final scope check over everything that actually changed in the copy.
        changed = changed_files(self.worktree)
        manifest = self.state.dependency.manifest_path
        unexpected = [p for p in changed if p != manifest and not is_allowed(p, allowed)]
        if unexpected:
            raise StageError(f"unexpected file changes outside plan: {unexpected}", blocked=True)
        self.state.changed_files = changed
        self.state.transition(JobStatus.PATCHED, f"{len(changed)} file(s) changed")

    def _verify(self) -> None:
        assert self.adapter is not None
        result = self.stages.verify(
            worktree=self.worktree,
            adapter=self.adapter,
            phase=VerificationPhase.UPGRADED,
            policy=self.job.policy,
            artifacts_dir=self.artifacts_dir,
        )
        self._record_commands(result)
        self.state.verification = result
        failure = _verification_failure(result, self.job.policy.require_tests)
        if failure is not None:
            status, reason = failure
            # Repair stage is not part of contract v0.1.0; failures are terminal for now.
            self.state.transition(status, reason)
            return
        sources_changed = [p for p in self.state.changed_files if p != self.state.dependency.manifest_path]
        if sources_changed:
            self.state.transition(JobStatus.VERIFIED, "upgraded install/configure/build/tests passed")
        else:
            self.state.transition(
                JobStatus.NO_CHANGES_NEEDED, "no impacted usages; upgraded build/tests passed"
            )

    # -- bookkeeping -----------------------------------------------------------

    def _record_commands(self, result: VerificationResult) -> None:
        for step in (result.dependency_install, result.configure, result.build, result.tests):
            if step is not None:
                self.state.command_results.append(step)

    def _stage_failure(self) -> JobStatus:
        return {
            JobStatus.INITIALIZED: JobStatus.BLOCKED,
            JobStatus.INSPECTED: JobStatus.FAILED_RESEARCH,
            JobStatus.RESEARCHED: JobStatus.FAILED_MIGRATION,
            JobStatus.IMPACT_ANALYZED: JobStatus.FAILED_MIGRATION,
            JobStatus.PLANNED: JobStatus.FAILED_MIGRATION,
            JobStatus.PATCHED: JobStatus.FAILED_BUILD,
        }.get(self.state.status, JobStatus.BLOCKED)

    def _fail(self, status: JobStatus, reason: str) -> None:
        if self.state.status in FAILURE_STATUSES:
            return
        for candidate in (status, JobStatus.BLOCKED):
            try:
                self.state.transition(candidate, reason)
                return
            except Exception:
                continue
        self.state.status_reason = reason  # already terminal; keep the explanation

    def _finalize(self) -> None:
        if self.worktree.joinpath(".git").is_dir():
            try:
                _write(self.artifacts_dir / "patches" / "final.diff", full_diff(self.worktree))
                if self.state.status in (JobStatus.VERIFIED, JobStatus.NO_CHANGES_NEEDED):
                    commit_all(
                        self.worktree,
                        f"depbot: upgrade {self.state.dependency.name} "
                        f"{self.state.dependency.current_version} -> {self.state.dependency.target_version}",
                    )
            except Exception as exc:  # evidence capture must not mask the job result
                log.warning("could not capture final diff for %s: %s", self.run_id, exc)
        try:
            path = self.stages.report(state=self.state, artifacts_dir=self.artifacts_dir)
            self.state.report_path = str(path)
        except Exception as exc:
            log.warning("report stage failed for %s: %s", self.run_id, exc)
        manifest = self.state.model_dump(mode="json")
        manifest["policy_decisions"] = self.policy_decisions
        _write(self.artifacts_dir / "state.json", json.dumps(manifest, indent=2))

    def _outcome(self, skipped_reason: str | None) -> RepositoryUpgradeOutcome:
        st = self.state
        if skipped_reason is not None:
            return RepositoryUpgradeOutcome(
                repository_id=st.repository_id,
                status=OutcomeStatus.SKIPPED,
                classification=FleetClassification.ALREADY_AT_TARGET_OR_NEWER,
                reason=skipped_reason,
                run_id=st.run_id,
                dependency_name=st.dependency.name,
                from_version=st.dependency.current_version,
                to_version=st.dependency.target_version,
            )
        status = _outcome_status(st.status)
        passed = bool(st.verification and st.verification.passed) and status in (
            OutcomeStatus.VERIFIED,
            OutcomeStatus.UNAFFECTED,
        )
        return RepositoryUpgradeOutcome(
            repository_id=st.repository_id,
            status=status,
            classification=FleetClassification.AFFECTED_CANDIDATE,
            reason=st.status_reason,
            run_id=st.run_id,
            dependency_name=st.dependency.name,
            from_version=st.dependency.current_version,
            to_version=st.dependency.target_version,
            changed_files=st.changed_files,
            report_path=st.report_path,
            verification_passed=passed,
        )


def run_repository_job(
    job: RepositoryUpgradeJob, stages: PipelineStages, *, artifacts_root: Path
) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
    return RepositoryJobRunner(job, stages, artifacts_root).run()
