"""Stage interfaces between the orchestrator (A) and stage owners (B, C).

Owner: Member A. Each Protocol is the exact callable signature the orchestrator
invokes. Stage owners implement a plain function with this signature in their
module (paths listed in CONTRACTS.md). Stages raise ``StageError`` for expected,
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
    from depbot.package_managers.base import PackageManagerAdapter
    from depbot.state import DepBotState


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
