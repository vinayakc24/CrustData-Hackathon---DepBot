"""Repository migration engine: runs ONE RepositoryUpgradeJob end to end.

Owner: Member A. Deterministic Python owns every transition; stages (B, C) are
injected callables that return typed values. Nothing here decides *which*
repositories run — that is ``depbot.fleet.runner``.

    INITIALIZED → INSPECTED (working copy, adapter, baseline) → RESEARCHED
    → IMPACT_ANALYZED → PLANNED (+ policy gate) → PATCHED (manifest + source)
    → VERIFIED | NO_CHANGES_NEEDED | FAILED_* | BLOCKED

Every transition is handed to an optional ``JobObserver`` (durable checkpoint), so
an interrupted job can resume from its last completed stage instead of starting
over; commands run through the job's ``ExecutionContext`` (local or container).
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib
import json
import logging
import secrets
import shutil
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from depbot.execution import ExecutionContext, current_context, use_execution
from depbot.interfaces import JobObserver, PipelineStages, SideEffectLedger, StageError
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
from depbot.llm.runtime import LLMFactory, current_llm_client, use_llm_client
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
    has_uncommitted_changes,
    prepare_working_copy,
)
from depbot.state import FAILURE_STATUSES, DepBotState, JobStatus
from depbot.telemetry import JobTelemetry, StageSpan, build_job_telemetry, sha256_text
from depbot.models import utc_now_iso

log = logging.getLogger(__name__)


def _probe_ignore(_dir: str, names: list[str]) -> set[str]:
    # Fresh build state and no Git metadata in the probe copy.
    return {n for n in names if n in (".git", "build", "CMakeUserPresets.json", "vcpkg_installed")}


class _SkipJob(Exception):
    """Revalidation found the repository no longer needs this upgrade."""


class _Cancelled(Exception):
    """The job's cancel token was set; stop at this stage boundary."""


# Progress order of the non-terminal statuses; a resumed job skips completed stages.
_PROGRESS = [
    JobStatus.INITIALIZED,
    JobStatus.INSPECTED,
    JobStatus.RESEARCHED,
    JobStatus.IMPACT_ANALYZED,
    JobStatus.PLANNED,
    JobStatus.PATCHED,
]
_DIFF_FACT = "checkpoint_diff_sha256"


def resume_decision(prior: DepBotState | None) -> tuple[bool, str]:
    """Can an interrupted job continue from ``prior`` (its last durable checkpoint)?

    Resumption is allowed only when the working copy on disk provably matches the
    checkpoint: untouched before PATCHED, and byte-identical to the recorded diff
    from PATCHED on. Anything else (missing copy, a patch applied half-way, a
    repair in flight) restarts the job from scratch in a fresh run directory.
    """
    if prior is None:
        return False, "no checkpoint recorded"
    if prior.status == JobStatus.INITIALIZED:
        return False, "interrupted before the first checkpoint"
    if prior.status in (JobStatus.FAILED_BUILD, JobStatus.FAILED_TESTS) and not prior.is_terminal:
        return False, "interrupted during a repair attempt"
    worktree = Path(prior.worktree_path)
    if not worktree.joinpath(".git").is_dir():
        return False, "working copy is missing"
    try:
        changed = changed_files(worktree)
        diff_hash = sha256_text(full_diff(worktree))
    except Exception as exc:
        return False, f"working copy is unreadable: {type(exc).__name__}: {exc}"
    patched = prior.status not in _PROGRESS or prior.status == JobStatus.PATCHED
    if not patched or _DIFF_FACT not in prior.repo_facts:
        if changed:
            return False, f"working copy has uncheckpointed edits: {changed[:5]}"
        return True, f"working copy is unmodified at checkpoint {prior.status.value}"
    if diff_hash != prior.repo_facts[_DIFF_FACT]:
        return False, "working copy differs from the checkpointed patch"
    return True, f"working copy matches the checkpointed patch at {prior.status.value}"


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
        *,
        observer: JobObserver | None = None,
        execution: ExecutionContext | None = None,
        ledger: SideEffectLedger | None = None,
        resume_state: DepBotState | None = None,
        attempt: int = 1,
        fleet_run_id: str | None = None,
    ) -> None:
        self.job = job
        self.stages = stages
        self.llm_factory = llm_factory
        self.observer = observer
        self.ledger = ledger
        self.attempt = attempt
        self.fleet_run_id = fleet_run_id
        self.spans: list[StageSpan] = []
        self.telemetry: JobTelemetry | None = None
        self.cancelled = False
        self.resumed_from: JobStatus | None = None
        self.adapter: PackageManagerAdapter | None = None
        self.policy_decisions: list[dict] = []
        self._applied_digests: set[str] = set()
        self._llm_client: object | None = None
        self._final_diff: str | None = None
        resumable, self.resume_note = resume_decision(resume_state)
        if resume_state is not None and resumable:
            self.state = resume_state.model_copy(deep=True)
            self.run_id = self.state.run_id
            self.artifacts_dir = Path(self.state.artifacts_dir)
            self.resumed_from = self.state.status
            self.policy_decisions = list(self.state.policy_decisions)
            self._applied_digests = {
                hashlib.sha256(p.unified_diff.encode()).hexdigest()
                for p in self.state.patches if p.status == PatchStatus.APPLIED
            }
        else:
            self.run_id = new_run_id(job)
            self.artifacts_dir = (artifacts_root / self.run_id).resolve()
            self.state = self._initial_state()
        # The job's private directory is the only location an isolating backend mounts read-write.
        self.execution = replace(execution or current_context(), workspace=self.artifacts_dir)

    def _initial_state(self) -> DepBotState:
        job = self.job
        record = job.dependency_record
        return DepBotState(
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

    @property
    def worktree(self) -> Path:
        return Path(self.state.worktree_path)

    # -- public ------------------------------------------------------------------

    def run(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        # Each job runs in its own copied context: an LLM client bound here (or by a
        # stage) can never leak into another job, even under concurrent execution.
        return contextvars.copy_context().run(self._run_in_context)

    def _run_in_context(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        with use_execution(self.execution):
            state, outcome = self._run_with_llm()
        self.telemetry = build_job_telemetry(
            job=self.job, state=state, outcome=outcome, spans=self.spans,
            execution=self.execution.backend.describe(), llm_client=self._llm_client,
            attempt=self.attempt, resumed_from=self.resumed_from.value if self.resumed_from else None,
            fleet_run_id=self.fleet_run_id, final_diff=self._final_diff,
        )
        try:
            _write(self.artifacts_dir / "telemetry.json", self.telemetry.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            log.warning("could not write telemetry for %s: %s", self.run_id, exc)
        if self.observer is not None:
            self.observer.finished(state, outcome, self.telemetry)
        return state, outcome

    def _run_with_llm(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        if self.llm_factory is None:
            self._llm_client = current_llm_client()
            return self._run()  # inherit whatever the caller bound (or nothing)
        try:
            client = self.llm_factory()
        except LLMError as exc:
            self._fail(JobStatus.BLOCKED, f"LLM client could not be created: {exc}")
            self._finalize()
            return self.state, self._outcome(None)
        self._llm_client = client
        with use_llm_client(client):
            return self._run()

    def _pipeline(self) -> list[tuple[str, object, int]]:
        """(stage name, callable, progress rank at which it runs)."""
        return [
            ("inspect", self._inspect, 0),
            ("probe", self._probe, 1),  # first: R1 (header API diff) needs the new version's compile database
            ("research", self._research, 1),
            ("impact", self._impact, 2),
            ("plan", self._plan, 3),
            ("policy_gate", self._gate_plan, 4),  # re-evaluated on resume: a gate is never skipped
            ("patch", self._patch, 4),
            ("verify", self._verify, 5),
        ]

    def _prepare_resume(self) -> int | None:
        """Progress rank to continue from, or None when the checkpoint is already terminal."""
        status = self.state.status
        log.info("job %s resuming from %s (%s)", self.run_id, status.value, self.resume_note)
        if status not in _PROGRESS:
            return None
        self.adapter = detect_adapter(self.worktree)
        if status == JobStatus.INSPECTED:  # the advisory probe copy may be half-built: redo it
            shutil.rmtree(self.artifacts_dir / "probe", ignore_errors=True)
        return _PROGRESS.index(status)

    def _stage(self, name: str, fn) -> None:
        if self.execution.cancel.is_set():
            raise _Cancelled(self.execution.cancel.reason)
        started_at, started = utc_now_iso(), time.monotonic()
        status, detail = "ok", None
        try:
            fn()
            if self.execution.cancel.is_set():
                raise _Cancelled(self.execution.cancel.reason)
        except BaseException as exc:
            if self.execution.cancel.is_set() and not isinstance(exc, _Cancelled):
                # Commands killed by cancellation look like stage failures; report the cause.
                status, detail = "cancelled", self.execution.cancel.reason
                raise _Cancelled(self.execution.cancel.reason) from exc
            status = ("cancelled" if isinstance(exc, _Cancelled)
                      else "blocked" if isinstance(exc, (_SkipJob, UnsafePath)) or getattr(exc, "blocked", False)
                      else "failed")
            detail = str(exc)[:500] or type(exc).__name__
            raise
        finally:
            span = StageSpan(stage=name, attempt=self.attempt, status=status, started_at=started_at,
                             duration_seconds=round(time.monotonic() - started, 3), detail=detail)
            self.spans.append(span)
            if self.observer is not None:
                self.observer.stage_finished(span)

    def _transition(self, status: JobStatus, note: str | None = None) -> None:
        """The only way this engine changes status: transition, then durable checkpoint."""
        self.state.transition(status, note)
        if self.observer is not None:
            self.observer.checkpoint(self.state, self.state.history[-1])

    def _run(self) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
        skipped_reason: str | None = None
        try:
            rank: int | None = 0
            if self.resumed_from is not None:
                rank = self._prepare_resume()
            elif self.observer is not None:
                self.observer.checkpoint(self.state, None)
            for name, fn, at in self._pipeline():
                if rank is not None and at >= rank:
                    self._stage(name, fn)
        except _Cancelled as exc:
            self.cancelled = True
            self._fail(JobStatus.BLOCKED, f"cancelled: {exc}")
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
                built = all(r is not None and r.succeeded for r in
                            (baseline.dependency_install, baseline.configure, baseline.build))
                if built and policy.require_tests and not baseline.tests_failed and not baseline.tests_passed:
                    raise StageError(
                        "baseline builds but has no tests; policy requires tests to verify an upgrade "
                        "(add a CTest test or set require_tests: false)",
                        blocked=True,
                    )
                raise StageError(
                    "baseline build/tests fail before any change; refusing to migrate "
                    "(cannot separate regressions from pre-existing failures)",
                    blocked=True,
                )
            self._record_compile_db()
        self._transition(JobStatus.INSPECTED)

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
        self._transition(JobStatus.RESEARCHED, f"{len(changes)} breaking change(s)")

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
        self._transition(JobStatus.IMPACT_ANALYZED, f"{len(usages)} usage(s)")

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
        self._transition(JobStatus.PLANNED, f"{len(plan)} planned edit(s)")

    def _gate_plan(self) -> None:
        plan = self.state.migration_plan
        decision = plan_confidence_gate(self.job.policy, self.state.breaking_changes, plan, self.state.usages)
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
        managed = {self.state.dependency.manifest_path, *self.adapter.lockfiles(self.worktree)}
        unexpected = [p for p in changed if p not in managed and not is_allowed(p, allowed)]
        if unexpected:
            raise StageError(f"unexpected file changes outside plan: {unexpected}", blocked=True)
        self.state.changed_files = changed
        # Fingerprint of the patched copy: resumption requires the disk to still match it.
        self.state.repo_facts[_DIFF_FACT] = sha256_text(full_diff(self.worktree))
        self._transition(JobStatus.PATCHED, f"{prefix}{len(changed)} file(s) changed")

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
            if self.execution.cancel.is_set():  # killed commands are not a verification verdict
                raise _Cancelled(self.execution.cancel.reason)
            status, reason = failure
            self._transition(status, reason)
            if not self._try_repair():
                return
            failure = self._run_upgraded_verification()
        sources_changed = [p for p in self.state.changed_files if p != self.state.dependency.manifest_path]
        if sources_changed:
            self._transition(JobStatus.VERIFIED, "upgraded install/configure/build/tests passed")
        else:
            self._transition(
                JobStatus.NO_CHANGES_NEEDED,
                "no source changes needed (no impacted usages); dependency bumped, upgraded build/tests passed"
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
        self._transition(JobStatus.PLANNED, f"repair attempt {attempt}: {len(patches)} patch(es)")
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
            elif self.ledger is not None:
                draft = publish_pull_request(draft, st, ledger=self.ledger)
            else:
                draft = publish_pull_request(draft, st)
            if draft.published and draft.url:
                st.pr_url = draft.url
                self._transition(JobStatus.PR_CREATED, f"PR opened: {draft.url}")
            else:
                self._transition(JobStatus.FAILED_PR, f"verified; PR not created: {draft.publish_note}")
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
                self._transition(candidate, reason)
                return
            except Exception:
                continue
        self.state.status_reason = reason  # already terminal; keep the explanation

    def _finalize(self) -> None:
        if self.worktree.joinpath(".git").is_dir():
            try:
                self._final_diff = full_diff(self.worktree)
                _write(self.artifacts_dir / "patches" / "final.diff", self._final_diff)
                # A resumed job whose earlier attempt already committed must not commit again.
                already = self.resumed_from is not None and not has_uncommitted_changes(self.worktree)
                if self.state.status in (JobStatus.VERIFIED, JobStatus.NO_CHANGES_NEEDED) and not already:
                    commit_all(
                        self.worktree,
                        f"depbot: upgrade {self.state.dependency.name} "
                        f"{self.state.dependency.current_version} -> {self.state.dependency.target_version}",
                    )
            except Exception as exc:  # evidence capture must not mask the job result
                log.warning("could not capture final diff for %s: %s", self.run_id, exc)
        if self.state.status in (JobStatus.VERIFIED, JobStatus.NO_CHANGES_NEEDED) and not self.cancelled:
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
    **options,
) -> tuple[DepBotState, RepositoryUpgradeOutcome]:
    """Run one job. ``llm_factory`` creates this job's own LLM client (see llm.runtime).
    ``options`` are ``RepositoryJobRunner``'s keyword arguments (observer, execution,
    ledger, resume_state, attempt, fleet_run_id)."""
    return RepositoryJobRunner(job, stages, artifacts_root, llm_factory, **options).run()
