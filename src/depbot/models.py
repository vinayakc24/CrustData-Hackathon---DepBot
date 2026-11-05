"""Authoritative shared data contracts for DepBot.

Owner: Member A (integration owner). Members B and C consume these models;
changes are proposed to A, never made independently. See CONTRACTS.md.

Data flow (each arrow is a typed hand-off):

    ReleaseEvent -> BreakingChange -> CodeUsage -> PlannedChange
        -> SourcePatch (validated) -> CommandResult -> VerificationResult
        -> RepositoryUpgradeOutcome -> FleetRunSummary
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "0.1.0"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Model(BaseModel):
    # Reject unknown fields so contract drift between members fails loudly.
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- enums


class EventType(StrEnum):
    RELEASE = "release"
    SECURITY_ADVISORY = "security_advisory"
    DEPRECATION = "deprecation"


class PackageManagerName(StrEnum):
    CONAN = "conan"
    VCPKG = "vcpkg"


class ChangeType(StrEnum):
    REMOVED = "removed"
    RENAMED = "renamed"
    SIGNATURE = "signature"
    BEHAVIOR = "behavior"
    DEPRECATED = "deprecated"
    UNKNOWN = "unknown"


class ChangeSource(StrEnum):
    """Which research layer produced a BreakingChange (see CONTRACTS.md §10)."""

    API_DIFF = "api_diff"  # R1: old vs new public headers (libclang)
    CHANGELOG_RULE = "changelog_rule"  # R2: deterministic release-note patterns
    LLM = "llm"  # R3: leftovers only, validated against the source text
    MANUAL = "manual"


class AnalysisMethod(StrEnum):
    """How a usage was found."""

    RIPGREP = "ripgrep"
    CLANG_AST = "clang_ast"
    COMPILER_DIAGNOSTIC = "compiler_diagnostic"  # from the probe build against the new version
    MANUAL = "manual"


class ResolutionLayer(StrEnum):
    """Which impact-cascade layer settled a usage's verdict (see CONTRACTS.md §9)."""

    TEXT_PREFILTER = "text_prefilter"  # I1: text match only, nothing proved yet
    CLANG_AST = "clang_ast"  # I2: USR resolution
    COMPILER_PROBE = "compiler_probe"  # I3: diagnostic from the probe build
    LLM = "llm"  # I4: residual classified by the LLM (never 'definite')
    HUMAN = "human"


class ImpactLevel(StrEnum):
    DEFINITE = "definite"
    POSSIBLE = "possible"
    UNAFFECTED = "unaffected"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ChangeLevel(StrEnum):
    """Semantic-version distance between current and target versions."""

    NONE = "none"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    UNKNOWN = "unknown"


class FleetClassification(StrEnum):
    """Fleet-selection verdict for one repository against one ReleaseEvent."""

    AFFECTED_CANDIDATE = "affected_candidate"
    UNAFFECTED = "unaffected"  # does not use the dependency
    ALREADY_AT_TARGET_OR_NEWER = "already_at_target_or_newer"
    DISABLED = "disabled"
    NEEDS_RESCAN = "needs_rescan"


class PatchStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    APPLIED = "applied"


class VerificationPhase(StrEnum):
    BASELINE = "baseline"
    PROBE = "probe"  # advisory build against the new version in a scratch copy (before planning)
    UPGRADED = "upgraded"


class OutcomeStatus(StrEnum):
    """Final per-repository status reported at fleet level."""

    SKIPPED = "skipped"  # selection decided no job was needed
    UNAFFECTED = "unaffected"  # job ran, found nothing to change, verified
    VERIFIED = "verified"  # migrated and verification passed
    BLOCKED = "blocked"  # policy/safety/precondition stopped the job
    FAILED = "failed"  # job ran and a stage failed


class MigrationVerification(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"


# ------------------------------------------------------------------ fleet inputs


class Dependency(_Model):
    name: str
    current_version: str
    target_version: str
    package_manager: PackageManagerName
    manifest_path: str  # repository-relative
    is_direct: bool = True


class RepositoryRecord(_Model):
    repository_id: str
    repo_url_or_path: str  # local path (relative to the fleet config) or Git URL
    display_name: str
    default_branch: str | None = None
    owner: str | None = None
    policy_id: str | None = None
    last_scanned_commit: str | None = None
    enabled: bool = True
    # Repository-relative paths DepBot may edit. Empty means "source dirs + manifest".
    allowed_paths: list[str] = Field(default_factory=list)


class ReleaseEvent(_Model):
    event_id: str
    dependency_name: str
    upstream_repo: str
    previous_version: str | None = None
    target_version: str
    event_type: EventType = EventType.RELEASE
    source_url: str | None = None
    # Local changelog/release-notes file for fixture-backed events (B consumes it).
    changelog_path: str | None = None
    detected_at: str = Field(default_factory=utc_now_iso)

    @property
    def dedup_key(self) -> tuple[str, str, str, str]:
        return (
            self.dependency_name,
            self.previous_version or "",
            self.target_version,
            self.event_type.value,
        )


class RepositoryDependencyRecord(_Model):
    """Inventory entry: repository X uses dependency D at version V."""

    repository_id: str
    dependency_name: str
    current_version: str
    package_manager: PackageManagerName
    manifest_path: str  # repository-relative
    is_direct: bool = True
    indexed_commit: str | None = None
    affected_symbols: list[str] = Field(default_factory=list)


class MigrationPattern(_Model):
    pattern_id: str
    dependency_name: str
    version_range: str
    old_symbols: list[str]
    replacement_summary: str
    evidence_urls: list[str]
    context_notes: str = ""
    verification_status: MigrationVerification
    source_run_id: str


class UpgradePolicy(_Model):
    policy_id: str = "default"
    allowed_change_levels: list[ChangeLevel] = Field(
        default_factory=lambda: [ChangeLevel.PATCH, ChangeLevel.MINOR]
    )
    require_tests: bool = True
    require_baseline: bool = True
    require_release_build: bool = False
    require_human_approval_for_major: bool = True
    # Explicit approval flag set by the operator (e.g. `--approve-major`).
    major_approved: bool = False
    minimum_migration_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    max_repair_attempts: int = Field(default=2, ge=0, le=3)
    run_probe_build: bool = True  # build against the new version before planning (impact layer I3)
    refuse_dirty_worktree: bool = True
    create_pr_on_failure: bool = False
    auto_merge: bool = False

    @field_validator("auto_merge")
    @classmethod
    def _no_auto_merge(cls, value: bool) -> bool:
        if value:
            raise ValueError("auto_merge is not supported; DepBot never merges PRs")
        return value


# ------------------------------------------------------------- research / impact


class BreakingChange(_Model):
    id: str  # stable within a run, e.g. "toydep-2.0.0-BC1"
    title: str
    change_type: ChangeType
    affected_symbols: list[str]
    old_behavior: str | None = None
    new_behavior: str | None = None
    replacement_symbols: list[str] = Field(default_factory=list)
    migration_advice: str
    evidence_urls: list[str]  # URL or file:// path of a retrieved source
    evidence_excerpt: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: ChangeSource | None = None  # provenance; None for legacy producers
    affected_usrs: list[str] = Field(default_factory=list)  # exact declarations (R1); used by AST impact

    @model_validator(mode="after")
    def _evidence_required(self) -> BreakingChange:
        if self.change_type != ChangeType.UNKNOWN and not self.evidence_urls:
            raise ValueError("a non-unknown breaking change must cite evidence_urls")
        return self


class CodeUsage(_Model):
    symbol: str
    file_path: str  # repository-relative, POSIX separators
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    snippet: str
    analysis_method: AnalysisMethod
    impact: ImpactLevel
    related_change_ids: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    # Impact-cascade provenance (optional; filled by layers that can prove something).
    resolved_by: ResolutionLayer | None = None
    resolution_proof: str | None = None  # e.g. "references c:@N@toydep@F@old_sum#I#I#"
    resolved_usr: str | None = None  # USR the reference resolves to (CLANG_AST)
    declaration_file: str | None = None  # where that declaration lives (dependency header)
    preprocessor_context: str | None = None  # e.g. "#ifdef _WIN32" for residuals

    @model_validator(mode="after")
    def _line_order(self) -> CodeUsage:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        if self.resolved_by == ResolutionLayer.LLM and self.impact == ImpactLevel.DEFINITE:
            raise ValueError("an LLM classification can never make a usage 'definite'")
        return self


# ------------------------------------------------------------------- migration


class PlannedChange(_Model):
    file_path: str  # repository-relative
    reason: str
    related_change_ids: list[str] = Field(min_length=1)
    related_usage_lines: list[int] = Field(default_factory=list)
    expected_change: str
    risk: RiskLevel = RiskLevel.LOW
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SourcePatch(_Model):
    """A unified diff plus its validation lifecycle."""

    patch_id: str
    unified_diff: str
    files: list[str]  # repository-relative paths the diff touches
    related_change_ids: list[str] = Field(default_factory=list)
    status: PatchStatus = PatchStatus.PROPOSED
    rejection_reason: str | None = None
    patch_path: str | None = None  # where the diff is stored under artifacts/


class ManifestChange(_Model):
    """Result of a package-manager adapter updating one direct dependency."""

    manifest_path: str
    dependency_name: str
    from_version: str
    to_version: str
    unified_diff: str


# ---------------------------------------------------------------- verification


class CommandResult(_Model):
    command_label: str  # e.g. "baseline.configure"
    args: list[str]
    cwd: str
    exit_code: int | None  # None when the process never produced one (timeout/not found)
    timed_out: bool = False
    duration_seconds: float = Field(ge=0.0)
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_tail: str = ""  # bounded excerpt for reports
    stderr_tail: str = ""
    output_truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class CompilerDiagnostic(_Model):
    """One structured compiler message (C's verification parses build output into these)."""

    file_path: str | None  # repository-relative; None if outside the worktree (e.g. dependency header)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    severity: str  # "error" | "warning" | "note" | "fatal error"
    message: str
    option: str | None = None  # e.g. "-Wdeprecated-declarations"
    command_label: str  # e.g. "probe.build"


class VerificationResult(_Model):
    """One verification pass (baseline or upgraded) of install/configure/build/test."""

    phase: VerificationPhase
    dependency_install: CommandResult | None = None
    configure: CommandResult | None = None
    build: CommandResult | None = None
    tests: CommandResult | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    failed_test_names: list[str] = Field(default_factory=list)
    passed: bool = False
    failure_category: str | None = None  # C's error-classifier label
    diagnostics: list[CompilerDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def _passed_needs_evidence(self) -> VerificationResult:
        if self.passed:
            steps = [self.dependency_install, self.configure, self.build]
            if any(step is None or not step.succeeded for step in steps):
                raise ValueError("passed=True requires successful install, configure and build")
        return self


# ------------------------------------------------------------------ fleet level


class RepositoryUpgradeJob(_Model):
    job_id: str
    event: ReleaseEvent
    repository: RepositoryRecord
    dependency_record: RepositoryDependencyRecord
    policy: UpgradePolicy
    repo_root: str  # absolute path of the source checkout (never modified)
    create_pr: bool = False  # publish to GitHub after the pre-PR gate; local artifact always


class RepositoryUpgradeOutcome(_Model):
    repository_id: str
    status: OutcomeStatus
    classification: FleetClassification
    reason: str | None = None
    run_id: str | None = None
    dependency_name: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    report_path: str | None = None
    pr_url: str | None = None
    verification_passed: bool = False

    @model_validator(mode="after")
    def _verified_needs_pass(self) -> RepositoryUpgradeOutcome:
        if self.status == OutcomeStatus.VERIFIED and not self.verification_passed:
            raise ValueError("status=verified requires verification_passed=True")
        return self


class FleetRunSummary(_Model):
    event_id: str
    dependency_name: str
    from_version: str | None
    to_version: str
    generated_at: str = Field(default_factory=utc_now_iso)
    repositories_considered: int
    repositories_affected: int
    repositories_skipped: int
    repositories_verified: int
    repositories_failed_or_blocked: int
    outcomes: list[RepositoryUpgradeOutcome]
    summary_path: str | None = None

    @classmethod
    def from_outcomes(
        cls, event: ReleaseEvent, outcomes: list[RepositoryUpgradeOutcome]
    ) -> FleetRunSummary:
        """Build a summary whose counts are derived from outcomes, never hand-set."""
        skipped = sum(o.status == OutcomeStatus.SKIPPED for o in outcomes)
        return cls(
            event_id=event.event_id,
            dependency_name=event.dependency_name,
            from_version=event.previous_version,
            to_version=event.target_version,
            repositories_considered=len(outcomes),
            repositories_affected=sum(
                o.classification == FleetClassification.AFFECTED_CANDIDATE for o in outcomes
            ),
            repositories_skipped=skipped,
            repositories_verified=sum(o.status == OutcomeStatus.VERIFIED for o in outcomes),
            repositories_failed_or_blocked=sum(
                o.status in (OutcomeStatus.FAILED, OutcomeStatus.BLOCKED) for o in outcomes
            ),
            outcomes=outcomes,
        )
