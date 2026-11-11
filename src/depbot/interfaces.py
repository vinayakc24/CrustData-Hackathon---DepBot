"""Stage interfaces between the orchestrator (A) and stage owners (B, C).

Owner: Member A. Each Protocol is the exact callable signature the orchestrator
invokes. Stage owners implement a plain function with this signature in their
module (paths listed in docs/CONTRACTS.md). Stages raise ``StageError`` for expected,
explained failures; any other exception is treated as a bug and fails the job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from depbot.models import (
    BreakingChange,
    CodeUsage,
    Dependency,
    PlannedChange,
    ReleaseEvent,
    SourcePatch,
    UpgradePolicy,
    VerificationPhase,
    VerificationResult,
)

if TYPE_CHECKING:
    from depbot.models import RepositoryUpgradeOutcome
    from depbot.package_managers.base import PackageManagerAdapter
    from depbot.state import DepBotState, StatusChange
    from depbot.telemetry import JobTelemetry, StageSpan


class StageError(RuntimeError):
    """Expected, explainable stage failure. ``blocked=True`` means a safety/policy stop."""

    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


# --- Member B --------------------------------------------------------------------


class ResearchStage(Protocol):
    """depbot.research.breaking_change_analyzer.research_release"""

    def __call__(
        self, *, event: ReleaseEvent, dependency: Dependency, artifacts_dir: Path
    ) -> list[BreakingChange]: ...


class ImpactStage(Protocol):
    """depbot.code_intelligence.impact_analyzer.find_impacted_usages

    Must search only inside ``worktree``; returned file paths are worktree-relative.
    An empty list is a valid result (nothing to migrate).
    """

    def __call__(
        self, *, worktree: Path, changes: list[BreakingChange], artifacts_dir: Path
    ) -> list[CodeUsage]: ...


# --- Member C --------------------------------------------------------------------


class PlanStage(Protocol):
    """depbot.migration.planner.plan_migration"""

    def __call__(
        self,
        *,
        dependency: Dependency,
        changes: list[BreakingChange],
        usages: list[CodeUsage],
    ) -> list[PlannedChange]: ...


class PatchStage(Protocol):
    """depbot.migration.patch_generator.generate_patches (source edits only;
    the manifest is updated by A's package-manager adapter)."""

    def __call__(
        self,
        *,
        worktree: Path,
        plan: list[PlannedChange],
        changes: list[BreakingChange],
        usages: list[CodeUsage],
    ) -> list[SourcePatch]: ...


class ApplyStage(Protocol):
    """depbot.migration.patch_validator.validate_and_apply

    Validates scope/paths, runs ``git apply --check``, applies, runs
    ``git diff --check``. Returns the patch with status APPLIED or REJECTED.
    """

    def __call__(
        self, *, worktree: Path, patch: SourcePatch, allowed_paths: list[str]
    ) -> SourcePatch: ...


class VerifyStage(Protocol):
    """depbot.verification.pipeline.run_verification

    Runs install -> configure -> build -> test with real subprocesses and
    returns observed results. Must never fabricate success.
    """

    def __call__(
        self,
        *,
        worktree: Path,
        adapter: PackageManagerAdapter,
        phase: VerificationPhase,
        policy: UpgradePolicy,
        artifacts_dir: Path,
    ) -> VerificationResult: ...


class ReportStage(Protocol):
    """depbot.reporting.report_generator.write_repository_report"""

    def __call__(self, *, state: DepBotState, artifacts_dir: Path) -> Path: ...


class RepairStage(Protocol):
    """depbot.verification.repair_engine.propose_repair  (optional, contract v0.2)

    Called after a FAILED_BUILD / FAILED_TESTS upgraded verification while repair
    attempts remain. Classify ``failure`` and return targeted source patches for
    the next attempt, or ``[]`` to give up. Must not run commands: the engine
    applies the patches through ``ApplyStage`` and re-runs full verification.
    ``attempt`` is 1-based. ``state`` is read-only for the stage.
    """

    def __call__(
        self,
        *,
        worktree: Path,
        state: DepBotState,
        failure: VerificationResult,
        attempt: int,
    ) -> list[SourcePatch]: ...


@dataclass(frozen=True)
class PipelineStages:
    """The stage callables one repository job uses. Injected so tests and the
    deterministic demo can supply fixture-backed research without changing code."""

    research: ResearchStage
    impact: ImpactStage
    plan: PlanStage
    patch: PatchStage
    apply: ApplyStage
    verify: VerifyStage
    report: ReportStage
    repair: RepairStage | None = None  # None: build/test failures are terminal


# --- Durability hooks (contract v0.9) ---------------------------------------------


class JobObserver(Protocol):
    """Receives one job's progress so it can be made durable and observable.

    The engine calls ``checkpoint`` after every state transition (the stage-level
    resume points), ``stage_finished`` after every stage attempt and ``finished``
    once with the final outcome. Implementations must not mutate ``state``.
    """

    def checkpoint(self, state: DepBotState, change: StatusChange | None) -> None: ...

    def stage_finished(self, span: StageSpan) -> None: ...

    def finished(
        self, state: DepBotState, outcome: RepositoryUpgradeOutcome, telemetry: JobTelemetry
    ) -> None: ...


class SideEffectLedger(Protocol):
    """Durable record of external side effects (e.g. a GitHub PR) keyed by a stable
    idempotency key, so a retried or resumed job never repeats one."""

    def completed(self, key: str) -> dict | None:
        """The recorded result if this effect already happened, else None."""

    def begin(self, key: str, kind: str) -> None: ...

    def complete(self, key: str, result: dict) -> None: ...
