"""Evidence-gated migration planning (Member C)."""

from __future__ import annotations

from depbot.migration.recipes import select_recipe
from depbot.models import BreakingChange, CodeUsage, Dependency, PlannedChange


def plan_migration(
    *,
    dependency: Dependency,
    changes: list[BreakingChange],
    usages: list[CodeUsage],
) -> list[PlannedChange]:
    """Plan only usages a registered recipe can migrate from documented evidence.

    A change is planned when exactly one recipe's preconditions hold for it and the
    recipe's matching strategy accepts the usage (proven impact, exact symbol).
    Possible, unknown and unaffected usages, and changes no recipe covers, are left
    for human review: this stage never guesses a mapping.
    """
    del dependency  # Dependency context is part of the contract; mappings are per change.
    by_id = {change.id: change for change in changes}
    recipes = {change.id: select_recipe(change) for change in changes}
    grouped: dict[tuple[str, str], list[CodeUsage]] = {}
    confidence: dict[tuple[str, str], float] = {}

    for usage in usages:
        for change_id in usage.related_change_ids:
            change, recipe = by_id.get(change_id), recipes.get(change_id)
            if change is None or recipe is None or not recipe.matches(change, usage):
                continue
            key = (usage.file_path, change_id)
            grouped.setdefault(key, []).append(usage)
            confidence[key] = min(confidence.get(key, 1.0), usage.confidence, change.confidence)

    plan: list[PlannedChange] = []
    for (file_path, change_id), entries in sorted(grouped.items()):
        change, recipe = by_id[change_id], recipes[change_id]
        old, new = change.affected_symbols[0].strip(), change.replacement_symbols[0].strip()
        plan.append(
            PlannedChange(
                file_path=file_path,
                reason=(
                    f"{change.id}: {old} is a definite impacted usage; release evidence "
                    f"documents {new} as its replacement (recipe {recipe.metadata.recipe_id} "
                    f"v{recipe.metadata.version})"
                ),
                related_change_ids=[change_id],
                related_usage_lines=sorted({entry.line_start for entry in entries}),
                expected_change=recipe.describe(change),
                risk=recipe.metadata.risk,
                confidence=confidence[(file_path, change_id)],
            )
        )
    return plan
