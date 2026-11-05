"""P0 deterministic impact stage backed by textual C/C++ symbol matches."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from depbot.code_intelligence.ripgrep_indexer import (
    RipgrepIndexError,
    UsageMatch,
    search_symbols,
)
from depbot.code_intelligence.symbol_extractor import SymbolCandidate, extract_symbols
from depbot.interfaces import StageError
from depbot.models import AnalysisMethod, BreakingChange, CodeUsage, ImpactLevel


def _safe_source_line(root: Path, match: UsageMatch) -> str:
    if not isinstance(match.file_path, str) or not isinstance(match.line_number, int) or not isinstance(match.snippet, str):
        raise StageError("indexer returned malformed match fields", blocked=True)
    rel = PurePosixPath(match.file_path)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or "\\" in match.file_path:
        raise StageError(f"indexer returned unsafe repository path: {match.file_path!r}", blocked=True)
    try:
        path = (root / Path(*rel.parts)).resolve(strict=True)
    except OSError as exc:
        raise StageError(f"indexer source path is unavailable: {match.file_path!r}", blocked=True) from exc
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StageError(f"indexer path escapes worktree: {match.file_path!r}", blocked=True) from exc
    if not path.is_file() or match.line_number < 1:
        raise StageError(f"indexer returned invalid source location: {match.file_path}:{match.line_number}", blocked=True)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise StageError(f"could not read indexed source {match.file_path!r}: {exc}") from exc
    if match.line_number > len(lines):
        raise StageError(f"indexer line is outside source file: {match.file_path}:{match.line_number}", blocked=True)
    actual = lines[match.line_number - 1]
    if actual[:1000] != match.snippet:
        raise StageError(f"indexer snippet does not match source at {match.file_path}:{match.line_number}", blocked=True)
    return actual


def find_impacted_usages(
    *, worktree: Path, changes: list[BreakingChange], artifacts_dir: Path
) -> list[CodeUsage]:
    """Return conservative possible usages found in the isolated worktree.

    Text matches alone cannot establish semantic impact, so every result is
    classified ``possible`` with confidence 0.5 pending contextual review.
    ``artifacts_dir`` is part of the frozen stage interface and is unused here.
    """
    del artifacts_dir
    try:
        root = Path(worktree).resolve(strict=True)
        if not root.is_dir():
            raise StageError(f"worktree is not a directory: {worktree}", blocked=True)
    except OSError as exc:
        raise StageError(f"worktree is unavailable: {worktree}: {exc}", blocked=True) from exc
    if not changes:
        return []
    candidates = extract_symbols(changes)
    if not candidates:
        return []

    change_ids = {candidate.symbol: list(candidate.related_change_ids) for candidate in candidates}
    try:
        matches = search_symbols(root, candidates)
    except RipgrepIndexError as exc:
        raise StageError(f"C/C++ usage indexing failed: {exc}", blocked=True) from exc
    except OSError as exc:
        raise StageError(f"C/C++ usage indexing could not access the worktree: {exc}", blocked=True) from exc

    usages: dict[tuple[str, str, int], CodeUsage] = {}
    for match in matches:
        if not isinstance(match, UsageMatch) or not isinstance(match.symbol, str) or match.symbol not in change_ids:
            raise StageError("indexer returned an inconsistent symbol match", blocked=True)
        snippet = _safe_source_line(root, match)
        usage = CodeUsage(
            symbol=match.symbol,
            file_path=match.file_path,
            line_start=match.line_number,
            line_end=match.line_number,
            snippet=snippet,
            analysis_method=AnalysisMethod.RIPGREP,
            impact=ImpactLevel.POSSIBLE,
            related_change_ids=change_ids[match.symbol],
            confidence=0.5,
        )
        key = (usage.symbol, usage.file_path, usage.line_start)
        previous = usages.get(key)
        if previous is not None and previous != usage:
            raise StageError(f"indexer returned conflicting duplicate match at {usage.file_path}:{usage.line_start}", blocked=True)
        usages[key] = usage
    return sorted(usages.values(), key=lambda item: (item.file_path, item.line_start, item.symbol))
