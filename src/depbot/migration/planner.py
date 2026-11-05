"""Evidence-gated migration planning (Member C)."""

from __future__ import annotations

from depbot.models import (
    BreakingChange,
    ChangeType,
    CodeUsage,
    Dependency,
    ImpactLevel,
    PlannedChange,
    RiskLevel,
)


def _unique_mapping(change: BreakingChange) -> tuple[str, str] | None:
    """Return an unambiguous old/new identifier pair, or None when review is needed."""
    if (
        change.change_type == ChangeType.UNKNOWN
        or len(change.affected_symbols) != 1
        or len(change.replacement_symbols) != 1
    ):
        return None
    old, new = change.affected_symbols[0].strip(), change.replacement_symbols[0].strip()
    if not old or not new or old == new or not change.evidence_urls:
        return None
    return old, new


def plan_migration(
    *,
    dependency: Dependency,
    changes: list[BreakingChange],
    usages: list[CodeUsage],
) -> list[PlannedChange]:
    """Plan only definite, evidence-backed usages with one documented replacement.

    Possible, unknown, and unaffected usages are left for human review. This stage
    does not guess mappings when a release record lists multiple symbols.
    """
    del dependency  # Dependency context is part of the contract; mappings are per change.
    by_id = {change.id: change for change in changes}
    grouped: dict[tuple[str, str, str, str], list[CodeUsage]] = {}
    confidence: dict[tuple[str, str, str, str], float] = {}

    for usage in usages:
        if usage.impact != ImpactLevel.DEFINITE:
            continue
        for change_id in usage.related_change_ids:
            change = by_id.get(change_id)
            mapping = _unique_mapping(change) if change else None
            if mapping is None or usage.symbol != mapping[0]:
                continue
            old, new = mapping
            key = (usage.file_path, change_id, old, new)
            grouped.setdefault(key, []).append(usage)
            confidence[key] = min(confidence.get(key, 1.0), usage.confidence, change.confidence)

    plan: list[PlannedChange] = []
    for (file_path, change_id, old, new), entries in sorted(grouped.items()):
        change = by_id[change_id]
        plan.append(
            PlannedChange(
                file_path=file_path,
                reason=(
                    f"{change.id}: {old} is a definite impacted usage; release evidence "
                    f"documents {new} as its replacement"
                ),
                related_change_ids=[change_id],
                related_usage_lines=sorted({entry.line_start for entry in entries}),
                expected_change=f"Replace identifier {old} with {new} at the cited usage lines only.",
                risk=RiskLevel.LOW,
                confidence=confidence[(file_path, change_id, old, new)],
            )
        )
    return plan
