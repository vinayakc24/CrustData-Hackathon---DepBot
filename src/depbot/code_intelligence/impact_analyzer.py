"""P0 deterministic impact stage backed by textual C/C++ symbol matches."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from depbot.code_intelligence.ripgrep_indexer import (
    RipgrepIndexError,
    UsageMatch,
    search_symbols,
)
from depbot.code_intelligence.symbol_extractor import SymbolCandidate, extract_symbols
from depbot.interfaces import StageError
from depbot.models import AnalysisMethod, BreakingChange, CodeUsage, ImpactLevel

_CALL_SYMBOL = r"(?<![\w]){}(?![\w])"
_NAMESPACE_SUFFIX = re.compile(r"(?:[A-Za-z_]\w*::)*$")
_EXPRESSION_PREFIX = re.compile(r"(?i)(?:\breturn|\bco_return|\bthrow|\bdelete|\bnew)\s*$")
_DECLARATION_TAIL = re.compile(
    r"(?:(?:static|inline|constexpr|virtual|extern|friend|explicit|consteval|constinit)\s+)*"
    r"(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:\s*<[^;{}]+>)?(?:\s*[*&])?\s*)+"
)


def _blank_non_code(text: str) -> str:
    """Mask C/C++ comments and quoted literals while preserving offsets/newlines."""
    chars = list(text)
    state = "code"
    escaped = False
    raw_end = ""
    index = 0

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if chars[position] not in "\r\n":
                chars[position] = " "

    while index < len(text):
        if state == "code":
            if text.startswith("//", index):
                blank(index, index + 2)
                index += 2
                state = "line_comment"
                continue
            if text.startswith("/*", index):
                blank(index, index + 2)
                index += 2
                state = "block_comment"
                continue
            if text.startswith('R"', index):
                opening = text.find("(", index + 2, min(index + 19, len(text)))
                if opening >= 0:
                    delimiter = text[index + 2:opening]
                    if not any(char.isspace() or char in "\\()" for char in delimiter):
                        raw_end = ")" + delimiter + '"'
                        blank(index, opening + 1)
                        index = opening + 1
                        state = "raw_string"
                        continue
            if text[index] in ('"', "'"):
                quote = text[index]
                blank(index, index + 1)
                index += 1
                state = "double_string" if quote == '"' else "single_string"
                escaped = False
                continue
        elif state == "line_comment":
            if text[index] in "\r\n":
                state = "code"
            else:
                blank(index, index + 1)
        elif state == "block_comment":
            if text.startswith("*/", index):
                blank(index, index + 2)
                index += 2
                state = "code"
                continue
            blank(index, index + 1)
        elif state in ("double_string", "single_string"):
            quote = '"' if state == "double_string" else "'"
            char = text[index]
            blank(index, index + 1)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                state = "code"
        elif state == "raw_string":
            if text.startswith(raw_end, index):
                blank(index, index + len(raw_end))
                index += len(raw_end)
                state = "code"
                continue
            blank(index, index + 1)
        index += 1
    return "".join(chars)


def _is_declaration_prefix(prefix: str) -> bool:
    tail = re.split(r"[;{}]", prefix)[-1].strip()
    if not tail or _EXPRESSION_PREFIX.search(tail):
        return False
    if tail.startswith("#"):
        return True
    tail = re.sub(r"\[\[[^\]]*\]\]\s*", "", tail)
    if tail[-1:] in "=([{,:?+-*/!%&|<>.":
        return False
    return bool(_DECLARATION_TAIL.fullmatch(tail))


def _is_clear_call(code_line: str, symbol: str) -> bool:
    pattern = re.compile(_CALL_SYMBOL.format(re.escape(symbol)))
    for match in pattern.finditer(code_line):
        if not re.match(r"\s*\(", code_line[match.end():]):
            continue
        prefix = code_line[:match.start()]
        namespace = _NAMESPACE_SUFFIX.search(prefix)
        if namespace:
            prefix = prefix[:namespace.start()]
        if not _is_declaration_prefix(prefix):
            return True
    return False


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
    """Return source matches, marking only clear call expressions as definite.

    Textual matches in comments, declarations, literals, or ambiguous syntax stay
    ``possible``. ``artifacts_dir`` is unused by this deterministic stage.
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
    source_cache: dict[str, tuple[list[str], list[str]]] = {}
    for match in matches:
        if not isinstance(match, UsageMatch) or not isinstance(match.symbol, str) or match.symbol not in change_ids:
            raise StageError("indexer returned an inconsistent symbol match", blocked=True)
        snippet = _safe_source_line(root, match)
        if match.file_path not in source_cache:
            source_path = root.joinpath(*PurePosixPath(match.file_path).parts)
            try:
                source_text = source_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise StageError(f"could not read indexed source {match.file_path!r}: {exc}") from exc
            clean_lines = _blank_non_code(source_text).splitlines()
            source_cache[match.file_path] = (source_text.splitlines(), clean_lines)
        raw_lines, clean_lines = source_cache[match.file_path]
        if match.line_number > len(clean_lines) or raw_lines[match.line_number - 1][:1000] != match.snippet:
            raise StageError(
                f"indexer snippet does not match source at {match.file_path}:{match.line_number}",
                blocked=True,
            )
        definite = _is_clear_call(clean_lines[match.line_number - 1], match.symbol)
        usage = CodeUsage(
            symbol=match.symbol,
            file_path=match.file_path,
            line_start=match.line_number,
            line_end=match.line_number,
            snippet=snippet,
            analysis_method=AnalysisMethod.RIPGREP,
            impact=ImpactLevel.DEFINITE if definite else ImpactLevel.POSSIBLE,
            related_change_ids=change_ids[match.symbol],
            confidence=0.95 if definite else 0.5,
        )
        key = (usage.symbol, usage.file_path, usage.line_start)
        previous = usages.get(key)
        if previous is not None and previous != usage:
            raise StageError(f"indexer returned conflicting duplicate match at {usage.file_path}:{usage.line_start}", blocked=True)
        usages[key] = usage
    return sorted(usages.values(), key=lambda item: (item.file_path, item.line_start, item.symbol))
