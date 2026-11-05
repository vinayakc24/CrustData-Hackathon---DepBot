"""Fleet selection: classify every registered repository against one ReleaseEvent.

Deterministic, no LLM, no builds. Only AFFECTED_CANDIDATE repositories whose
policy gate allows migration become jobs; everything else gets a recorded reason.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.registry import Fleet, RegistryError
from depbot.fleet.versions import UnparseableVersion, compare
from depbot.models import (
    FleetClassification,
    OutcomeStatus,
    ReleaseEvent,
    RepositoryDependencyRecord,
    RepositoryRecord,
    RepositoryUpgradeOutcome,
)
from depbot.policy.engine import PolicyDecision, pre_migration_gate


class RepositorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: RepositoryRecord
    classification: FleetClassification
    reason: str
    repo_root: str | None = None
    dependency_record: RepositoryDependencyRecord | None = None
    rescanned: bool = False
    policy_decision: PolicyDecision | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return (
            self.classification == FleetClassification.AFFECTED_CANDIDATE
            and self.policy_decision is not None
            and self.policy_decision.allowed
        )

    def to_outcome(self, event: ReleaseEvent) -> RepositoryUpgradeOutcome:
        """Outcome for a repository that will NOT get a job (skip/block)."""
        rec = self.dependency_record
        if self.classification == FleetClassification.AFFECTED_CANDIDATE:
            status = OutcomeStatus.BLOCKED  # affected but stopped by policy
            reason = "; ".join(self.policy_decision.reasons) if self.policy_decision else self.reason
        elif self.classification == FleetClassification.NEEDS_RESCAN:
            status, reason = OutcomeStatus.BLOCKED, self.reason
        else:
            status, reason = OutcomeStatus.SKIPPED, self.reason
        return RepositoryUpgradeOutcome(
            repository_id=self.repository.repository_id,
            status=status,
            classification=self.classification,
            reason=reason,
            dependency_name=event.dependency_name,
            from_version=rec.current_version if rec else None,
            to_version=event.target_version,
        )


def _classify(
    event: ReleaseEvent, repo: RepositoryRecord, fleet: Fleet, index: DependencyIndex
) -> RepositorySelection:
    if not repo.enabled:
        return RepositorySelection(
            repository=repo,
            classification=FleetClassification.DISABLED,
            reason="repository is disabled in the fleet registry",
        )
    try:
        root = fleet.local_root(repo)
    except RegistryError as exc:
        return RepositorySelection(
            repository=repo, classification=FleetClassification.NEEDS_RESCAN, reason=str(exc)
        )
    if not root.is_dir():
        return RepositorySelection(
            repository=repo,
            classification=FleetClassification.NEEDS_RESCAN,
            reason=f"repository path does not exist: {root}",
        )

    scan, rescanned = index.refresh(repo, root)
    base = {"repository": repo, "repo_root": str(root), "rescanned": rescanned}
    if scan.error:
        return RepositorySelection(
            **base,
            classification=FleetClassification.NEEDS_RESCAN,
            reason=f"inventory scan refused: {scan.error}",
        )
    matches = [d for d in scan.dependencies if d.dependency_name == event.dependency_name]
    if not matches:
        return RepositorySelection(
            **base,
            classification=FleetClassification.UNAFFECTED,
            reason=f"does not declare {event.dependency_name} as a direct dependency",
        )
    if len(matches) > 1:
        versions = ", ".join(m.current_version for m in matches)
        return RepositorySelection(
            **base,
            classification=FleetClassification.NEEDS_RESCAN,
            reason=f"{event.dependency_name} declared {len(matches)} times ({versions}); ambiguous",
        )
    record = matches[0]
    try:
        cmp = compare(record.current_version, event.target_version)
    except UnparseableVersion as exc:
        return RepositorySelection(
            **base,
            classification=FleetClassification.NEEDS_RESCAN,
            dependency_record=record,
            reason=str(exc),
        )
    if cmp >= 0:
        return RepositorySelection(
            **base,
            classification=FleetClassification.ALREADY_AT_TARGET_OR_NEWER,
            dependency_record=record,
            reason=(
                f"already on {event.dependency_name} {record.current_version} "
                f"(target {event.target_version}); no migration needed"
            ),
        )
    decision = pre_migration_gate(fleet.policy_for(repo), record.current_version, event.target_version)
    return RepositorySelection(
        **base,
        classification=FleetClassification.AFFECTED_CANDIDATE,
        dependency_record=record,
        policy_decision=decision,
        reason=(
            f"declares {event.dependency_name} {record.current_version} in "
            f"{record.manifest_path}; target {event.target_version}"
        ),
    )


def select_repositories(
    event: ReleaseEvent, fleet: Fleet, index: DependencyIndex
) -> list[RepositorySelection]:
    """Classify all registered repositories. Persists any rescanned inventory."""
    selections = [_classify(event, repo, fleet, index) for repo in fleet.repositories]
    index.save()
    return selections


def state_dir_for(fleet: Fleet, override: Path | None = None) -> Path:
    return (override or fleet.base_dir / ".depbot").resolve()
