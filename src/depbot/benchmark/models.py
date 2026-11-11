"""Benchmark registry schema and result models.

A case pins everything needed to reproduce one migration (repository content,
dependency, versions, release notes, verification policy) and states the ground
truth: which breaking changes exist, which usages they affect, what the correct
outcome is and what the migrated lines must look like. Ground truth describes a
*correct* migration; it is never derived from DepBot's own output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from depbot.models import ChangeType, OutcomeStatus, PackageManagerName

REGISTRY_SCHEMA_VERSION = 1
RESULTS_SCHEMA_VERSION = 1
Requirement = Literal["conan", "cmake", "git", "network", "libclang"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositorySpec(_Model):
    """Either an in-tree fixture pinned by content hash, or a Git URL pinned by commit."""

    path: str | None = None  # relative to the registry file
    tree_sha256: str | None = None
    url: str | None = None
    commit: str | None = None

    @model_validator(mode="after")
    def _pinned(self) -> RepositorySpec:
        if (self.path is None) == (self.url is None):
            raise ValueError("give exactly one of repository.path or repository.url")
        if self.path is not None and not self.tree_sha256:
            raise ValueError("repository.path must be pinned with tree_sha256")
        if self.url is not None and not self.commit:
            raise ValueError("repository.url must be pinned with commit")
        return self


class ExpectedChange(_Model):
    affected_symbols: list[str] = Field(min_length=1)
    replacement_symbols: list[str] = Field(default_factory=list)
    change_type: ChangeType | None = None  # reported separately; not part of the match


class ExpectedUsage(_Model):
    file: str
    line: int = Field(ge=1)
    symbol: str


class ExpectedLine(_Model):
    line: int = Field(ge=1)
    text: str  # the migrated line, compared without trailing whitespace


class Expected(_Model):
    breaking_changes: list[ExpectedChange] = Field(default_factory=list)
    affected_usages: list[ExpectedUsage] = Field(default_factory=list)
    outcome: OutcomeStatus
    also_acceptable: list[OutcomeStatus] = Field(default_factory=list)  # equally correct outcomes
    # Source file → lines it must contain after a correct migration. Its keys are
    # exactly the source files a correct migration edits (manifest/lockfiles excluded).
    patched_lines: dict[str, list[ExpectedLine]] = Field(default_factory=dict)
    absent_text: dict[str, list[str]] = Field(default_factory=dict)  # file → text that must be gone

    @model_validator(mode="after")
    def _consistent(self) -> Expected:
        if OutcomeStatus.VERIFIED in self.also_acceptable:
            raise ValueError("'verified' cannot be a merely acceptable outcome; make it the expected one")
        if self.outcome != OutcomeStatus.VERIFIED and (self.patched_lines or self.absent_text):
            raise ValueError("only a case whose correct outcome is 'verified' may expect patched lines")
        return self


class VerificationSpec(_Model):
    require_tests: bool = True
    build_configurations: list[Literal["Release", "Debug"]] = Field(default_factory=lambda: ["Release"])


class Setup(_Model):
    conan_recipes: list[str] = Field(default_factory=list)  # recipe dirs, relative to the registry file


class BenchmarkCase(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    description: str
    split: Literal["dev", "held_out"] = "dev"
    tags: list[str] = Field(default_factory=list)
    repository: RepositorySpec
    dependency: str
    package_manager: PackageManagerName = PackageManagerName.CONAN
    baseline_version: str
    target_version: str
    upstream_repo: str = "local/benchmark"
    changelog: str | None = None  # release notes, relative to the registry file
    setup: Setup = Field(default_factory=Setup)
    requires: list[Requirement] = Field(default_factory=lambda: ["conan", "cmake", "git"])
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    expected: Expected


class BenchmarkRegistry(_Model):
    model_config = ConfigDict(extra="ignore")  # top-level ``x-*`` keys hold YAML anchors

    schema_version: Literal[1]
    suite: str
    suite_version: str
    cases: list[BenchmarkCase]

    @model_validator(mode="after")
    def _unique(self) -> BenchmarkRegistry:
        ids = [case.id for case in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case id: {dupes}")
        return self


class BenchmarkError(ValueError):
    pass


def load_registry(path: Path) -> BenchmarkRegistry:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BenchmarkError(f"cannot read benchmark registry {path}: {exc}") from exc
    try:
        return BenchmarkRegistry.model_validate(data)
    except ValueError as exc:
        raise BenchmarkError(f"invalid benchmark registry {path}: {exc}") from exc


# ----------------------------------------------------------------------- results


class Counts(_Model):
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    def __add__(self, other: Counts) -> Counts:
        return Counts(tp=self.tp + other.tp, fp=self.fp + other.fp, fn=self.fn + other.fn)


class CaseResult(_Model):
    case_id: str
    split: str
    status: Literal["completed", "not_run", "error"]
    reason: str | None = None  # why it was not run / what went wrong
    expected_outcome: str
    actual_outcome: str | None = None
    job_status: str | None = None
    outcome_reason: str | None = None
    outcome_match: bool | None = None
    # False when the job legitimately stopped before that stage (e.g. a manifest refusal):
    # the stage made no prediction, so it is not scored.
    research_ran: bool = False
    impact_ran: bool = False
    breaking_changes: Counts = Field(default_factory=Counts)
    change_type_mismatches: list[str] = Field(default_factory=list)
    impact: Counts = Field(default_factory=Counts)
    patch_correct: bool | None = None  # None: no patch is expected for this case
    patch_problems: list[str] = Field(default_factory=list)
    verification_expected: bool = False
    verification_passed: bool = False
    abstention_expected: bool = False
    abstained: bool | None = None
    false_verified: bool = False
    duration_seconds: float | None = None
    llm_calls: int = 0
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_cost_usd: float | None = None
    run_id: str | None = None
    artifacts_dir: str | None = None


class Aggregate(_Model):
    cases: int = 0
    completed: int = 0
    not_run: int = 0
    errors: int = 0
    breaking_change_precision: float | None = None
    breaking_change_recall: float | None = None
    impact_precision: float | None = None
    impact_recall: float | None = None
    outcome_accuracy: float | None = None
    patch_correctness: float | None = None
    verification_success: float | None = None
    safe_abstention_accuracy: float | None = None
    false_verified: int = 0
    duration_seconds: float = 0.0
    llm_calls: int = 0
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_cost_usd: float | None = None


class BenchmarkResults(_Model):
    schema_version: int = RESULTS_SCHEMA_VERSION
    suite: str
    suite_version: str
    generated_at: str
    depbot_version: str
    depbot_commit: str | None = None
    llm: str = "none"
    splits_run: list[str]
    toolchain: dict[str, str | None]
    execution: dict[str, str | None]
    aggregate: dict[str, Aggregate]  # "overall", "dev", "held_out"
    cases: list[CaseResult]
