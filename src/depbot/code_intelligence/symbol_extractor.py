"""Deterministically map affected API symbols to their breaking changes."""

from __future__ import annotations

from dataclasses import dataclass

from depbot.models import BreakingChange


@dataclass(frozen=True)
class SymbolCandidate:
    """Exact affected symbol with the unique changes that refer to it."""

    symbol: str
    related_change_ids: tuple[str, ...]


def extract_symbols(changes: list[BreakingChange]) -> list[SymbolCandidate]:
    """Return first-seen affected symbols and their ordered change IDs.

    Symbols are preserved verbatim. Replacement symbols are deliberately
    excluded because they describe migration targets, not old APIs to search.
    """
    mapping: dict[str, list[str]] = {}
    for change in changes:
        for symbol in change.affected_symbols:
            if not symbol or not symbol.strip():
                continue
            change_ids = mapping.setdefault(symbol, [])
            if change.id not in change_ids:
                change_ids.append(change.id)
    return [SymbolCandidate(symbol, tuple(change_ids)) for symbol, change_ids in mapping.items()]
