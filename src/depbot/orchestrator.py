"""Repository migration engine: runs ONE RepositoryUpgradeJob end to end.

Owner: Member A. Deterministic Python owns every transition; stages (B, C) are
injected callables that return typed values. Nothing here decides *which*
repositories run — that is ``depbot.fleet.runner``.

    INITIALIZED → INSPECTED (working copy, adapter, baseline) → RESEARCHED
    → IMPACT_ANALYZED → PLANNED (+ policy gate) → PATCHED (manifest + source)
    → VERIFIED | NO_CHANGES_NEEDED | FAILED_* | BLOCKED
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib
import json
import logging
import secrets
import shutil
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
    SourcePatch,
    VerificationPhase,
    VerificationResult,
)
from depbot.package_managers.base import PackageManagerAdapter, PackageManagerError
from depbot.package_managers.detector import detect_adapter
from depbot.fleet.versions import UnparseableVersion, compare
from depbot.llm.base import LLMError
from depbot.llm.runtime import LLMFactory, use_llm_client
from depbot.policy.engine import plan_confidence_gate
from depbot.reporting.pull_request import prepare_pull_request, publish_pull_request
from depbot.repository.compile_db import (
    external_include_dirs,
    find_compile_commands,
    load_compile_commands,
)
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


def _probe_ignore(_dir: str, names: list[str]) -> set[str]:
    # Fresh build state and no Git metadata in the probe copy.
    return {n for n in names if n in (".git", "build", "CMakeUserPresets.json", "vcpkg_installed")}


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


_OPTIONAL_STAGE_TARGETS = {
    "repair": "depbot.verification.repair_engine:propose_repair",
}


def _import(target: str):
    module_name, func_name = target.split(":")
    return getattr(importlib.import_module(module_name), func_name)


def load_default_stages() -> PipelineStages:
    """Resolve B's and C's stage functions; missing ones block jobs honestly.
    Missing optional stages (repair) simply disable that behaviour."""
    resolved = {}
    for stage, target in _STAGE_TARGETS.items():
        try:
            resolved[stage] = _import(target)
        except (ImportError, AttributeError):
            resolved[stage] = _not_integrated(stage, target.replace(":", "."))
    for stage, target in _OPTIONAL_STAGE_TARGETS.items():
        try:
            resolved[stage] = _import(target)
        except (ImportError, AttributeError):
            resolved[stage] = None
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
        names = ", ".join(result.failed_test_names)
        detail = f"; failed tests: {names}" if names else ""
        return JobStatus.FAILED_TESTS, f"tests {failed} (exit code {result.tests.exit_code}){detail}"
    if not result.passed:
        return JobStatus.FAILED_TESTS, "verification stage reported passed=False"
    return None


def _outcome_status(status: JobStatus) -> OutcomeStatus:
    # FAILED_PR: the upgrade itself verified; only GitHub delivery failed (reason says so).
    if status in (JobStatus.VERIFIED, JobStatus.PR_CREATED, JobStatus.FAILED_PR):
        return OutcomeStatus.VERIFIED
    if status == JobStatus.NO_CHANGES_NEEDED:
        return OutcomeStatus.UNAFFECTED
    if status == JobStatus.BLOCKED:
        return OutcomeStatus.BLOCKED
    return OutcomeStatus.FAILED


# ---------------------------------------------------------------------------- engine


class RepositoryJobRunner:
    def __init__(
        self,
        job: RepositoryUpgradeJob,
        stages: PipelineStages,
        artifacts_root: Path,
        llm_factory: LLMFactory | None = None,
    ) -> None:
        self.job = job
        self.stages = stages
        self.llm_factory = llm_factory
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
        self._applied_digests: set[str] = set()

    @property
    def worktree(self) -> Path:
        return Path(self.state.worktree_path)

    # -- public ------------------------------------------------------------------

    def run(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        # Each job runs in its own copied context: an LLM client bound here (or by a
        # stage) can never leak into another job, even under concurrent execution.
        return contextvars.copy_context().run(self._run_with_llm)

    def _run_with_llm(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        if self.llm_factory is None:
            return self._run()  # inherit whatever the caller bound (or nothing)
        try:
            client = self.llm_factory()
        except LLMError as exc:
            self._fail(JobStatus.BLOCKED, f"LLM client could not be created: {exc}")
            self._finalize()
            return self.state, self._outcome(None)
        with use_llm_client(client):
            return self._run()

    def _run(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        skipped_reason: str | None = None
        try:
            self._inspect()
            self._probe()  # first: R1 (header API diff) needs the new version's compile database
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
        except LLMError as exc:  # incl. LLMNotConfigured: an explained, not internal, failure
            self._fail(self._stage_failure(), f"LLM: {exc}")
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
        problems = self.adapter.environment_problems()
        if problems:
            raise StageError(f"{self.adapter.name.value} tooling: " + "; ".join(problems), blocked=True)
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
            self._record_compile_db()
        self.state.transition(JobStatus.INSPECTED)

    def _record_compile_db(self) -> None:
        """Keep the baseline compile database for the impact stage's AST layer."""
        found = find_compile_commands(self.worktree)
        if found is None:
            self.state.repo_facts["compile_commands"] = None
            return
        saved = self.artifacts_dir / "compile_commands.baseline.json"
        shutil.copyfile(found, saved)
        try:
            dep_dirs = external_include_dirs(load_compile_commands(saved), self.worktree)
        except (ValueError, OSError) as exc:
            dep_dirs = []
            log.warning("unreadable compile database %s: %s", found, exc)
        self.state.repo_facts["compile_commands"] = {
            "baseline": str(saved),
            "worktree": str(found),
            "external_include_dirs": dep_dirs,
        }

    def _probe(self) -> None:
        """Build a scratch copy against the target version before research and planning.
        Its compile database feeds R1 (header API diff) and its diagnostics feed impact layer I3.
        Advisory only: it never changes the job status and never touches the worktree."""
        assert self.adapter is not None
        facts: dict[str, object] = {"ran": False}
        self.state.repo_facts["probe"] = facts
        if not self.job.policy.run_probe_build:
            facts["skipped"] = "disabled by policy (run_probe_build=false)"
            return
        probe_dir = self.artifacts_dir / "probe"
        probe_wt = probe_dir / "worktree"
        try:
            shutil.copytree(self.worktree, probe_wt, symlinks=True, ignore=_probe_ignore)
            self.adapter.update_dependency(
                probe_wt, self.state.dependency.name, self.state.dependency.target_version
            )
            result = self.stages.verify(
                worktree=probe_wt,
                adapter=self.adapter,
                phase=VerificationPhase.PROBE,
                policy=self.job.policy,
                artifacts_dir=probe_dir,
            )
        except (PackageManagerError, StageError, OSError) as exc:
            facts["skipped"] = f"probe could not run: {exc}"
            return
        self.state.probe = result
        self._record_commands(result)
        diagnostics_path = probe_dir / "diagnostics.json"
        _write(
            diagnostics_path,
            json.dumps([d.model_dump(mode="json") for d in result.diagnostics], indent=2),
        )
        probe_db = find_compile_commands(probe_wt)
        saved_db = None
        if probe_db is not None:
            saved_db = probe_dir / "compile_commands.json"
            shutil.copyfile(probe_db, saved_db)
        facts.update(
            ran=True,
            build_succeeded=bool(result.build and result.build.succeeded),
            errors=sum(d.severity in ("error", "fatal error") for d in result.diagnostics),
            diagnostics_path=str(diagnostics_path),
            compile_commands=str(saved_db) if saved_db else None,
            worktree=str(probe_wt),
        )

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
        decision_data = decision.model_dump(mode="json")
        self.policy_decisions.append(decision_data)
        self.state.policy_decisions.append(decision_data)
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

        if self.state.migration_plan:
            patches = self.stages.patch(
                worktree=self.worktree,
                plan=self.state.migration_plan,
                changes=self.state.breaking_changes,
                usages=self.state.usages,
            )
            self._apply_patches(patches)
        self._check_scope_and_transition("")

    def _apply_patches(self, patches: list[SourcePatch]) -> None:
        allowed = self._allowed_paths()
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
            digest = hashlib.sha256(patch.unified_diff.encode()).hexdigest()
            if digest in self._applied_digests:
                raise StageError(f"patch {patch.patch_id} repeats an already-applied diff; stopping")
            applied = self.stages.apply(worktree=self.worktree, patch=patch, allowed_paths=allowed)
            self.state.patches.append(applied)
            if applied.status != PatchStatus.APPLIED:
                raise StageError(
                    f"patch {patch.patch_id} not applied: {applied.rejection_reason or applied.status}"
                )
            self._applied_digests.add(digest)
            _write(self.artifacts_dir / "patches" / f"{patch.patch_id}.diff", patch.unified_diff)

    def _check_scope_and_transition(self, prefix: str) -> None:
        # Final scope check over everything that actually changed in the copy.
        allowed = self._allowed_paths()
        changed = changed_files(self.worktree)
        manifest = self.state.dependency.manifest_path
        unexpected = [p for p in changed if p != manifest and not is_allowed(p, allowed)]
        if unexpected:
            raise StageError(f"unexpected file changes outside plan: {unexpected}", blocked=True)
        self.state.changed_files = changed
        self.state.transition(JobStatus.PATCHED, f"{prefix}{len(changed)} file(s) changed")

    def _run_upgraded_verification(self) -> tuple[JobStatus, str] | None:
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
        return _verification_failure(result, self.job.policy.require_tests)

    def _verify(self) -> None:
        failure = self._run_upgraded_verification()
        while failure is not None:
            status, reason = failure
            self.state.transition(status, reason)
            if not self._try_repair():
                return
            failure = self._run_upgraded_verification()
        sources_changed = [p for p in self.state.changed_files if p != self.state.dependency.manifest_path]
        if sources_changed:
            self.state.transition(JobStatus.VERIFIED, "upgraded install/configure/build/tests passed")
        else:
            self.state.transition(
                JobStatus.NO_CHANGES_NEEDED, "no impacted usages; upgraded build/tests passed"
            )

    def _try_repair(self) -> bool:
        """One bounded repair attempt. True if a repair was applied and must be re-verified.
        Leaves the state in its FAILED_* status when no repair is possible."""
        st = self.state
        if self.stages.repair is None or st.status not in (JobStatus.FAILED_BUILD, JobStatus.FAILED_TESTS):
            return False
        if st.repair_attempts >= st.max_repair_attempts:
            st.status_reason = f"{st.status_reason} (repair limit {st.max_repair_attempts} reached)"
            return False
        assert st.verification is not None
        attempt = st.repair_attempts + 1
        patches = self.stages.repair(
            worktree=self.worktree, state=st, failure=st.verification, attempt=attempt
        )
        if not patches:
            st.repair_attempt_details.append({"attempt": attempt, "patches": []})
            st.status_reason = f"{st.status_reason} (repair attempt {attempt}: no repair proposed)"
            return False
        st.transition(JobStatus.PLANNED, f"repair attempt {attempt}: {len(patches)} patch(es)")
        patch_start = len(st.patches)
        try:
            self._apply_patches(patches)
        finally:
            results = {patch.patch_id: patch for patch in st.patches[patch_start:]}
            st.repair_attempt_details.append({
                "attempt": attempt,
                "patches": [
                    {
                        "patch_id": patch.patch_id,
                        "paths": list(patch.files),
                        "artifact_path": (
                            str(self.artifacts_dir / "patches" / f"{patch.patch_id}.diff")
                            if (self.artifacts_dir / "patches" / f"{patch.patch_id}.diff").is_file()
                            else None
                        ),
                        "status": results[patch.patch_id].status.value if patch.patch_id in results else None,
                        "rejection_reason": (
                            results[patch.patch_id].rejection_reason
                            if patch.patch_id in results else None
                        ),
                    }
                    for patch in patches
                ],
            })
        self._check_scope_and_transition(f"repair attempt {attempt}: ")
        return True

    def _deliver(self) -> None:
        """Local PR artifact always (if the gate allows); GitHub only when requested."""
        st = self.state
        copy = st.repo_facts.get("working_copy") or {}
        base = self.job.repository.default_branch or copy.get("source_branch")
        try:
            draft = prepare_pull_request(st, self.job.policy, self.artifacts_dir, base_branch=base)
        except Exception as exc:
            log.warning("could not write PR artifact for %s: %s", self.run_id, exc)
            return
        if draft is None:
            st.repo_facts["pull_request"] = {"skipped": "pre-PR policy gate did not allow a PR"}
            return
        if self.job.create_pr and st.status == JobStatus.VERIFIED:
            if copy.get("mode") != "clone":
                draft = draft.model_copy(
                    update={"publish_note": "working copy is a snapshot (not a Git root); cannot publish"}
                )
            else:
                draft = publish_pull_request(draft, st)
            if draft.published and draft.url:
                st.pr_url = draft.url
                st.transition(JobStatus.PR_CREATED, f"PR opened: {draft.url}")
            else:
                st.transition(JobStatus.FAILED_PR, f"verified; PR not created: {draft.publish_note}")
        st.repo_facts["pull_request"] = draft.model_dump(mode="json")

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
        if self.state.status in (JobStatus.VERIFIED, JobStatus.NO_CHANGES_NEEDED):
            self._deliver()
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
            pr_url=st.pr_url,
            verification_passed=passed,
        )


def run_repository_job(
    job: RepositoryUpgradeJob,
    stages: PipelineStages,
    *,
    artifacts_root: Path,
    llm_factory: LLMFactory | None = None,
) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
    """Run one job. ``llm_factory`` creates this job's own LLM client (see llm.runtime)."""
    return RepositoryJobRunner(job, stages, artifacts_root, llm_factory).run()
