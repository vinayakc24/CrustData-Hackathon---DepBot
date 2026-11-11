"""Typed migration-recipe contract.

A recipe is a deterministic, code-defined source transformation for one class of
breaking change. It never executes anything and never accepts free-form edits: an
LLM may *propose* a ``BreakingChange``, but only a registered recipe turns one
into text edits, and only after its preconditions hold. The resulting diff still
goes through ``patch_validator`` (scope, ``git apply --check``) and full verification.

Lifecycle, driven by the planner and the patch generator::

    preconditions(change)  -> unmet reasons ([] = recipe applies)
    matches(change, usage) -> may this recorded usage be edited?
    transform(...)         -> new lines + touched line indexes (or StageError when stale)
    postconditions(...)    -> violations ([] = edit is exactly what was promised)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from depbot.models import BreakingChange, ChangeSource, ChangeType, CodeUsage, ImpactLevel, RiskLevel

# A deleted line: ``splitlines(keepends=True)`` never yields "", so it is unambiguous.
DELETED = ""


@dataclass(frozen=True)
class VerificationRequirements:
    """What must pass before an edit made by this recipe may count as verified."""

    build: bool = True
    tests: bool = False  # True: refuse to run under a policy that does not require tests


@dataclass(frozen=True)
class RecipeMetadata:
    recipe_id: str
    version: str
    title: str
    description: str
    change_types: frozenset[ChangeType]
    risk: RiskLevel = RiskLevel.LOW
    requires_ast: bool = False
    verification: VerificationRequirements = field(default_factory=VerificationRequirements)


@dataclass(frozen=True)
class Provenance:
    """Why an edit is justified: the change it implements and the evidence behind it."""

    recipe_id: str
    recipe_version: str
    change_id: str
    source: ChangeSource | None
    evidence_urls: tuple[str, ...]
    evidence_excerpt: str | None
    confidence: float


@dataclass(frozen=True)
class TransformResult:
    lines: list[str]  # same length as the input; removed lines are ``DELETED``
    touched: set[int]  # 0-based indexes that were edited or deleted


def unique_mapping(change: BreakingChange) -> tuple[str, str] | None:
    """The single documented old → new pair, or None when a human must decide."""
    if (
        change.change_type == ChangeType.UNKNOWN
        or len(change.affected_symbols) != 1
        or len(change.replacement_symbols) != 1
        or not change.evidence_urls
    ):
        return None
    old, new = change.affected_symbols[0].strip(), change.replacement_symbols[0].strip()
    return (old, new) if old and new and old != new else None


class MigrationRecipe(ABC):
    metadata: RecipeMetadata

    def preconditions(self, change: BreakingChange) -> list[str]:
        """Reasons this recipe must not handle ``change``. Subclasses extend, never relax."""
        unmet = []
        if change.change_type not in self.metadata.change_types:
            unmet.append(f"change type {change.change_type.value} is not handled by {self.metadata.recipe_id}")
        if not change.evidence_urls:
            unmet.append("the change cites no evidence")
        if unique_mapping(change) is None:
            unmet.append("the change does not document exactly one replacement")
        return unmet

    def provenance(self, change: BreakingChange) -> Provenance:
        return Provenance(
            recipe_id=self.metadata.recipe_id, recipe_version=self.metadata.version, change_id=change.id,
            source=change.source, evidence_urls=tuple(change.evidence_urls),
            evidence_excerpt=change.evidence_excerpt, confidence=change.confidence,
        )

    def matches(self, change: BreakingChange, usage: CodeUsage) -> bool:
        """Matching strategy. Default: a proven (definite) usage of the affected symbol."""
        mapping = unique_mapping(change)
        return (mapping is not None and usage.impact == ImpactLevel.DEFINITE and usage.symbol == mapping[0]
                and change.id in usage.related_change_ids)

    @abstractmethod
    def describe(self, change: BreakingChange) -> str:
        """One sentence stating the exact edit, recorded in the plan."""

    @abstractmethod
    def transform(self, *, lines: list[str], change: BreakingChange, usages: list[CodeUsage],
                  file_path: str) -> TransformResult:
        """Edit ``lines`` at the recorded usages only. Raise ``StageError(blocked=True)``
        when the source no longer matches the recorded evidence."""

    @abstractmethod
    def postconditions(self, *, before: list[str], after: list[str], touched: set[int],
                       change: BreakingChange) -> list[str]:
        """Violations of what the recipe promised (``after`` still has ``DELETED`` slots)."""
