"""Per-repository job state and explicit stage transitions.

Owner: Member A. Fleet-level state never lives here: one DepBotState describes
exactly one repository job (plan.md §3.7, §5.4).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from depbot.models import (
    BreakingChange,
    CodeUsage,
    CommandResult,
    Dependency,
    ManifestChange,
    PlannedChange,
    SourcePatch,
    VerificationResult,
    utc_now_iso,
)


class JobStatus(StrEnum):
    INITIALIZED = "INITIALIZED"
    INSPECTED = "INSPECTED"
    RESEARCHED = "RESEARCHED"
    IMPACT_ANALYZED = "IMPACT_ANALYZED"
    PLANNED = "PLANNED"
    PATCHED = "PATCHED"
    VERIFIED = "VERIFIED"
    PR_CREATED = "PR_CREATED"
    # Terminal success without edits: no impacted usages, upgraded build/tests passed.
    NO_CHANGES_NEEDED = "NO_CHANGES_NEEDED"
    # Failure statuses
    BLOCKED = "BLOCKED"
    FAILED_RESEARCH = "FAILED_RESEARCH"
    FAILED_MIGRATION = "FAILED_MIGRATION"
    FAILED_INSTALL = "FAILED_INSTALL"
    FAILED_BUILD = "FAILED_BUILD"
    FAILED_TESTS = "FAILED_TESTS"
    FAILED_PR = "FAILED_PR"


FAILURE_STATUSES = frozenset(
    {
        JobStatus.BLOCKED,
        JobStatus.FAILED_RESEARCH,
        JobStatus.FAILED_MIGRATION,
        JobStatus.FAILED_INSTALL,
        JobStatus.FAILED_BUILD,
        JobStatus.FAILED_TESTS,
        JobStatus.FAILED_PR,
    }
)
TERMINAL_STATUSES = FAILURE_STATUSES | {
    JobStatus.VERIFIED,
    JobStatus.PR_CREATED,
    JobStatus.NO_CHANGES_NEEDED,
}

_FAIL_ANY = {JobStatus.BLOCKED}
ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.INITIALIZED: {JobStatus.INSPECTED} | _FAIL_ANY,
    JobStatus.INSPECTED: {JobStatus.RESEARCHED, JobStatus.FAILED_RESEARCH} | _FAIL_ANY,
    JobStatus.RESEARCHED: {JobStatus.IMPACT_ANALYZED} | _FAIL_ANY,
    JobStatus.IMPACT_ANALYZED: {JobStatus.PLANNED, JobStatus.FAILED_MIGRATION} | _FAIL_ANY,
    JobStatus.PLANNED: {JobStatus.PATCHED, JobStatus.FAILED_MIGRATION} | _FAIL_ANY,
    JobStatus.PATCHED: {
        JobStatus.VERIFIED,
        JobStatus.NO_CHANGES_NEEDED,
        JobStatus.FAILED_INSTALL,
        JobStatus.FAILED_BUILD,
        JobStatus.FAILED_TESTS,
    }
    | _FAIL_ANY,
    # Bounded repair loop (attempt counter enforced in transition()).
    JobStatus.FAILED_BUILD: {JobStatus.IMPACT_ANALYZED, JobStatus.PLANNED},
    JobStatus.FAILED_TESTS: {JobStatus.IMPACT_ANALYZED, JobStatus.PLANNED},
    JobStatus.VERIFIED: {JobStatus.PR_CREATED, JobStatus.FAILED_PR},
}
REPAIR_EDGES = {
    (JobStatus.FAILED_BUILD, JobStatus.IMPACT_ANALYZED),
    (JobStatus.FAILED_BUILD, JobStatus.PLANNED),
    (JobStatus.FAILED_TESTS, JobStatus.IMPACT_ANALYZED),
    (JobStatus.FAILED_TESTS, JobStatus.PLANNED),
}


class InvalidTransition(RuntimeError):
    pass


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_status: JobStatus
    to_status: JobStatus
    at: str = Field(default_factory=utc_now_iso)
    note: str | None = None


class DepBotState(BaseModel):
    """Mutable state of one repository upgrade job. Only the orchestrator transitions it."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    repository_id: str
    original_repo: str
    worktree_path: str
    base_branch: str
    working_branch: str
    dependency: Dependency
    artifacts_dir: str
    repo_facts: dict[str, object] = Field(default_factory=dict)
    upstream_repo: str | None = None
    breaking_changes: list[BreakingChange] = Field(default_factory=list)
    usages: list[CodeUsage] = Field(default_factory=list)
    migration_plan: list[PlannedChange] = Field(default_factory=list)
    patches: list[SourcePatch] = Field(default_factory=list)
    manifest_change: ManifestChange | None = None
    changed_files: list[str] = Field(default_factory=list)
    command_results: list[CommandResult] = Field(default_factory=list)
    baseline: VerificationResult | None = None
    verification: VerificationResult | None = None
    repair_attempts: int = 0
    max_repair_attempts: int = 2
    report_path: str | None = None
    pr_url: str | None = None
    status: JobStatus = JobStatus.INITIALIZED
    status_reason: str | None = None
    history: list[StatusChange] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES and not self._repair_possible()

    def _repair_possible(self) -> bool:
        return (
            self.status in (JobStatus.FAILED_BUILD, JobStatus.FAILED_TESTS)
            and self.repair_attempts < self.max_repair_attempts
        )

    def transition(self, new_status: JobStatus, note: str | None = None) -> None:
        allowed = ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise InvalidTransition(f"{self.status} -> {new_status} is not allowed")
        if (self.status, new_status) in REPAIR_EDGES:
            if self.repair_attempts >= self.max_repair_attempts:
                raise InvalidTransition(
                    f"repair limit reached ({self.repair_attempts}/{self.max_repair_attempts})"
                )
            self.repair_attempts += 1
        self.history.append(
            StatusChange(from_status=self.status, to_status=new_status, note=note)
        )
        self.status = new_status
        if note is not None:
            self.status_reason = note
