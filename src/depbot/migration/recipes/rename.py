"""Recipe 1: replace one identifier with its documented replacement."""

from __future__ import annotations

import re

from depbot.interfaces import StageError
from depbot.migration.recipes.base import MigrationRecipe, RecipeMetadata, TransformResult, unique_mapping
from depbot.models import BreakingChange, ChangeType, CodeUsage


def _identifier(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")


class SymbolRenameRecipe(MigrationRecipe):
    metadata = RecipeMetadata(
        recipe_id="symbol-rename",
        version="1.0.0",
        title="Identifier rename",
        description="Replace a removed/renamed/deprecated identifier with its single documented "
                    "replacement, at proven usage lines only.",
        change_types=frozenset({ChangeType.RENAMED, ChangeType.REMOVED, ChangeType.DEPRECATED,
                                ChangeType.SIGNATURE, ChangeType.BEHAVIOR}),
    )

    def describe(self, change: BreakingChange) -> str:
        old, new = unique_mapping(change)  # type: ignore[misc]  (preconditions hold)
        return f"Replace identifier {old} with {new} at the cited usage lines only."

    def transform(self, *, lines: list[str], change: BreakingChange, usages: list[CodeUsage],
                  file_path: str) -> TransformResult:
        old, new = unique_mapping(change)  # type: ignore[misc]
        pattern, after, touched = _identifier(old), list(lines), set()
        for usage in usages:
            if usage.line_start != usage.line_end:
                continue  # multi-line evidence needs review; do not widen the edit.
            index = usage.line_start - 1
            if index < 0 or index >= len(after):
                raise StageError(f"stale usage line {file_path}:{usage.line_start}", blocked=True)
            current = after[index]
            found = list(pattern.finditer(current))
            if not found:
                # An already migrated line is not a reason to generate another patch.
                if _identifier(new).search(current):
                    continue
                raise StageError(
                    f"recorded symbol {old!r} is absent from {file_path}:{usage.line_start}", blocked=True
                )
            if len(found) != 1:
                raise StageError(
                    f"ambiguous occurrence count for {old!r} at {file_path}:{usage.line_start}", blocked=True
                )
            if usage.snippet.strip() and usage.snippet.strip() not in current:
                raise StageError(
                    f"recorded source snippet changed at {file_path}:{usage.line_start}", blocked=True
                )
            after[index] = pattern.sub(new, current, count=1)
            touched.add(index)
        return TransformResult(after, touched)

    def postconditions(self, *, before: list[str], after: list[str], touched: set[int],
                       change: BreakingChange) -> list[str]:
        old, new = unique_mapping(change)  # type: ignore[misc]
        pattern, problems = _identifier(old), []
        for index in sorted(touched):
            if pattern.sub(new, before[index], count=1) != after[index]:
                problems.append(f"line {index + 1} changed by more than the {old} -> {new} substitution")
        return problems
