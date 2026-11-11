"""Recipe registry: the closed set of transformations DepBot may apply.

Order matters: the first recipe whose preconditions hold handles a change, so
specific recipes come before general ones.
"""

from __future__ import annotations

from depbot.migration.recipes.base import (
    DELETED,
    MigrationRecipe,
    Provenance,
    RecipeMetadata,
    TransformResult,
    VerificationRequirements,
    unique_mapping,
)
from depbot.migration.recipes.header_relocation import HeaderRelocationRecipe
from depbot.migration.recipes.rename import SymbolRenameRecipe
from depbot.models import BreakingChange, PlannedChange, UpgradePolicy

__all__ = [
    "DELETED", "MigrationRecipe", "Provenance", "RecipeMetadata", "RecipeRegistry", "TransformResult",
    "VerificationRequirements", "default_registry", "select_recipe", "unique_mapping",
    "verification_requirement_problems",
]


class RecipeRegistry:
    def __init__(self, recipes: list[MigrationRecipe] | None = None) -> None:
        self._recipes: list[MigrationRecipe] = []
        for recipe in recipes or []:
            self.register(recipe)

    def register(self, recipe: MigrationRecipe, *, first: bool = False) -> None:
        if not isinstance(recipe, MigrationRecipe):
            raise TypeError("only MigrationRecipe instances can be registered")
        if any(r.metadata.recipe_id == recipe.metadata.recipe_id for r in self._recipes):
            raise ValueError(f"duplicate recipe id: {recipe.metadata.recipe_id}")
        self._recipes.insert(0, recipe) if first else self._recipes.append(recipe)

    @property
    def recipes(self) -> tuple[MigrationRecipe, ...]:
        return tuple(self._recipes)

    def select(self, change: BreakingChange) -> MigrationRecipe | None:
        return next((r for r in self._recipes if not r.preconditions(change)), None)

    def explain(self, change: BreakingChange) -> dict[str, list[str]]:
        """Why each recipe does or does not apply (for reports and debugging)."""
        return {r.metadata.recipe_id: r.preconditions(change) for r in self._recipes}


_DEFAULT = RecipeRegistry([HeaderRelocationRecipe(), SymbolRenameRecipe()])


def default_registry() -> RecipeRegistry:
    return _DEFAULT


def select_recipe(change: BreakingChange) -> MigrationRecipe | None:
    return _DEFAULT.select(change)


def verification_requirement_problems(
    policy: UpgradePolicy, changes: list[BreakingChange], plan: list[PlannedChange],
    registry: RecipeRegistry | None = None,
) -> list[str]:
    """Planned edits whose recipe needs stronger verification than the policy will run."""
    registry = registry or _DEFAULT
    by_id = {change.id: change for change in changes}
    problems: list[str] = []
    for item in plan:
        for change_id in item.related_change_ids:
            change = by_id.get(change_id)
            recipe = registry.select(change) if change is not None else None
            if recipe is not None and recipe.metadata.verification.tests and not policy.require_tests:
                problems.append(
                    f"{item.file_path}: recipe {recipe.metadata.recipe_id} requires tests, "
                    "but the policy has require_tests=false"
                )
    return sorted(set(problems))
