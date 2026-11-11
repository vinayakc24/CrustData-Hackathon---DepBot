"""Recipe 2: a public header moved (``#include <lib/old.hpp>`` → ``<lib/new.hpp>``).

Not a text substitution: only real ``#include`` directives are rewritten (never a
comment or string that mentions the path), the directive's delimiter style,
indentation, trailing comment and line ending are preserved, and when the file
already includes the new header the stale directive is removed instead of
producing a duplicate include.
"""

from __future__ import annotations

import re

from depbot.interfaces import StageError
from depbot.migration.recipes.base import (
    DELETED,
    MigrationRecipe,
    RecipeMetadata,
    TransformResult,
    unique_mapping,
)
from depbot.models import BreakingChange, ChangeType, CodeUsage, ImpactLevel

_HEADER = re.compile(r"^[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*\.(?:h|hh|hpp|hxx|h\+\+|inc|ipp|tcc|tpp)$")


def is_header_path(symbol: str) -> bool:
    return bool(_HEADER.fullmatch(symbol)) and ".." not in symbol.split("/")


def _directive(header: str) -> re.Pattern[str]:
    """``#include <header>`` or ``#include "header"`` with matching delimiters."""
    return re.compile(
        r'^(?P<head>[ \t]*#[ \t]*include[ \t]*)(?:<%s>|"%s")(?P<tail>.*?)(?P<eol>\r?\n?)$'
        % (re.escape(header), re.escape(header)),
        re.DOTALL,
    )


class HeaderRelocationRecipe(MigrationRecipe):
    metadata = RecipeMetadata(
        recipe_id="header-relocation",
        version="1.0.0",
        title="Relocated public header",
        description="Rewrite #include directives of a header that moved; drop the old directive "
                    "when the new header is already included.",
        change_types=frozenset({ChangeType.RENAMED, ChangeType.REMOVED, ChangeType.DEPRECATED}),
    )

    def preconditions(self, change: BreakingChange) -> list[str]:
        unmet = super().preconditions(change)
        mapping = unique_mapping(change)
        if mapping is not None and not (is_header_path(mapping[0]) and is_header_path(mapping[1])):
            unmet.append("the affected and replacement symbols are not both header paths")
        return unmet

    def matches(self, change: BreakingChange, usage: CodeUsage) -> bool:
        mapping = unique_mapping(change)
        # The directive itself is the proof: a compiler must have confirmed it (definite).
        return (mapping is not None and usage.symbol == mapping[0] and usage.impact == ImpactLevel.DEFINITE
                and change.id in usage.related_change_ids)

    def describe(self, change: BreakingChange) -> str:
        old, new = unique_mapping(change)  # type: ignore[misc]
        return (f"Rewrite #include of {old} to {new} at the cited lines only "
                f"(remove it where {new} is already included).")

    def transform(self, *, lines: list[str], change: BreakingChange, usages: list[CodeUsage],
                  file_path: str) -> TransformResult:
        old, new = unique_mapping(change)  # type: ignore[misc]
        old_directive, new_directive = _directive(old), _directive(new)
        after, touched = list(lines), set()
        cited = {usage.line_start - 1 for usage in usages if usage.line_start == usage.line_end}
        has_new = any(new_directive.match(line) for line in after)
        for index in sorted(cited):
            if index < 0 or index >= len(after):
                raise StageError(f"stale usage line {file_path}:{index + 1}", blocked=True)
            match = old_directive.match(after[index])
            if match is None:
                if new_directive.match(after[index]):
                    continue  # already migrated
                raise StageError(
                    f"{file_path}:{index + 1} is not an #include of {old!r}; refusing to edit it", blocked=True
                )
            if has_new:
                after[index] = DELETED
            else:
                opener, closer = ("<", ">") if f"<{old}>" in after[index] else ('"', '"')
                after[index] = f"{match['head']}{opener}{new}{closer}{match['tail']}{match['eol']}"
                has_new = True
            touched.add(index)
        return TransformResult(after, touched)

    def postconditions(self, *, before: list[str], after: list[str], touched: set[int],
                       change: BreakingChange) -> list[str]:
        old, new = unique_mapping(change)  # type: ignore[misc]
        old_directive, new_directive = _directive(old), _directive(new)
        problems = []
        for index in sorted(touched):
            if not old_directive.match(before[index]):
                problems.append(f"line {index + 1} was edited but was not an #include of {old}")
            elif after[index] != DELETED and not new_directive.match(after[index]):
                problems.append(f"line {index + 1} does not include {new} after the edit")
        live = [line for line in after if line != DELETED]
        if touched and sum(bool(new_directive.match(line)) for line in live) < 1:
            problems.append(f"{new} is not included after the edit")
        gained = sum(bool(new_directive.match(l)) for l in live) - sum(bool(new_directive.match(l)) for l in before)
        if gained > 1:
            problems.append(f"the edit introduced duplicate includes of {new}")
        return problems
