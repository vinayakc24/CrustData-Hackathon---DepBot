"""Extension point for Clang AST-based recipes.

Text recipes edit a line the impact cascade already proved. Changes that need
syntax — reordering call arguments, wrapping an expression, qualifying a name —
must work from the AST. Subclass ``AstMigrationRecipe`` and implement
``edits_for``; the base class turns source ranges into line edits, enforces that
libclang is available (fail closed: no AST, no edit) and requires tests, because
an AST rewrite can compile and still change behaviour.

No AST recipe ships yet; this module is the contract a new one plugs into.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from depbot.clang_support import ClangUnavailable, load_cindex
from depbot.interfaces import StageError
from depbot.migration.recipes.base import MigrationRecipe, TransformResult
from depbot.models import BreakingChange, CodeUsage


@dataclass(frozen=True)
class SourceEdit:
    """Replace ``[column_start, column_end)`` (0-based) on one 1-based line."""

    line: int
    column_start: int
    column_end: int
    replacement: str


def apply_edits(lines: list[str], edits: list[SourceEdit], file_path: str) -> TransformResult:
    """Apply non-overlapping single-line edits right-to-left so columns stay valid."""
    after, touched = list(lines), set()
    ordered = sorted(edits, key=lambda e: (e.line, e.column_start), reverse=True)
    previous: SourceEdit | None = None
    for edit in ordered:
        index = edit.line - 1
        if index < 0 or index >= len(after) or not 0 <= edit.column_start <= edit.column_end <= len(after[index]):
            raise StageError(f"AST edit is outside {file_path}:{edit.line}", blocked=True)
        if previous is not None and previous.line == edit.line and edit.column_end > previous.column_start:
            raise StageError(f"overlapping AST edits at {file_path}:{edit.line}", blocked=True)
        after[index] = after[index][:edit.column_start] + edit.replacement + after[index][edit.column_end:]
        touched.add(index)
        previous = edit
    return TransformResult(after, touched)


class AstMigrationRecipe(MigrationRecipe):
    """Base for recipes that compute edits from a libclang translation unit.

    ``metadata`` must set ``requires_ast=True`` and ``verification.tests=True``.
    """

    def preconditions(self, change: BreakingChange) -> list[str]:
        unmet = super().preconditions(change)
        if not self.metadata.requires_ast or not self.metadata.verification.tests:
            unmet.append("an AST recipe must declare requires_ast and require tests")
        try:
            load_cindex()
        except ClangUnavailable as exc:
            unmet.append(f"libclang is unavailable: {exc}")
        return unmet

    @abstractmethod
    def edits_for(self, *, source: str, change: BreakingChange, usages: list[CodeUsage],
                  file_path: str) -> list[SourceEdit]:
        """Parse ``source`` and return the edits for the cited usages."""

    def transform(self, *, lines: list[str], change: BreakingChange, usages: list[CodeUsage],
                  file_path: str) -> TransformResult:
        edits = self.edits_for(source="".join(lines), change=change, usages=usages, file_path=file_path)
        cited = {usage.line_start for usage in usages}
        stray = sorted({edit.line for edit in edits} - cited)
        if stray:
            raise StageError(f"AST recipe proposed edits outside the cited usages: {file_path}:{stray}",
                             blocked=True)
        return apply_edits(lines, edits, file_path)
