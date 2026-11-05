"""Resolve textual candidates to compiler declarations with libclang USRs (I2)."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path, PurePosixPath

from depbot.clang_support import ClangUnavailable, load_cindex, overloaded_decls
from depbot.models import (
    AnalysisMethod,
    BreakingChange,
    CodeUsage,
    ImpactLevel,
    ResolutionLayer,
)
from depbot.repository.compile_db import CompileCommand, load_compile_commands

log = logging.getLogger(__name__)


def _absolute(path: str | Path) -> Path:
    return Path(path).resolve(strict=False)


def _normalize_flag_path(value: str, directory: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else (directory / path).resolve(strict=False))


def _is_source_argument(value: str, source: Path, directory: Path) -> bool:
    path = Path(value)
    return (path if path.is_absolute() else directory / path).resolve(strict=False) == source


def _clang_args(entry: CompileCommand) -> list[str]:
    """Keep semantic flags, remove compiler/output arguments, and absolutize paths."""
    args = list(entry.arguments)
    if args and not args[0].startswith("-"):
        args = args[1:]
    directory = _absolute(entry.directory)
    source = _absolute(entry.file)
    result: list[str] = []
    paired_paths = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "/I", "/FI"}
    output_flags = {"-o", "-MF", "-MT", "-MQ", "-MJ", "/Fo"}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in output_flags:
            index += 2
            continue
        if arg in {"-c", "-MD", "-MMD", "-MP", "/c"}:
            index += 1
            continue
        if arg in paired_paths:
            result.append(arg)
            if index + 1 < len(args):
                result.append(_normalize_flag_path(args[index + 1], directory))
            index += 2
            continue
        if arg.startswith(("-I", "-isystem", "-iquote", "-idirafter", "/I", "/FI")) and len(arg) > 2:
            for flag in ("-idirafter", "-isystem", "-iquote", "-I", "/I", "/FI"):
                if arg.startswith(flag) and len(arg) > len(flag):
                    normalized_flag = "-I" if flag == "/I" else ("-include" if flag == "/FI" else flag)
                    result.append(normalized_flag + _normalize_flag_path(arg[len(flag):], directory))
                    break
            index += 1
            continue
        if arg.startswith(("-o", "-MF", "-MT", "-MQ", "-MJ")) and len(arg) > 2:
            index += 1
            continue
        if arg.startswith("/Fo") or _is_source_argument(arg, source, directory):
            index += 1
            continue
        result.append(arg)
        index += 1
    return result


def _file_name(location) -> str | None:
    file = getattr(location, "file", None)
    return str(_absolute(file.name)) if file is not None and getattr(file, "name", None) else None


def _cursor_usrs(cursor, ci) -> set[str]:
    if cursor.kind == ci.CursorKind.OVERLOADED_DECL_REF:
        try:
            return {decl.get_usr() for decl in overloaded_decls(cursor) if decl.get_usr()}
        except Exception:
            return set()
    referenced = getattr(cursor, "referenced", None)
    usr = referenced.get_usr() if referenced is not None else ""
    return {usr} if usr else set()


def _usage_targets(usage: CodeUsage, changes: list[BreakingChange]) -> set[str]:
    related = set(usage.related_change_ids)
    return {
        usr for change in changes if change.id in related
        for usr in change.affected_usrs if usr
    }


def _preprocessor_context(source: Path, line_number: int) -> str | None:
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    active: list[str] = []
    for line in lines[:max(0, line_number - 1)]:
        directive = line.strip()
        if directive.startswith(("#if ", "#ifdef ", "#ifndef ")):
            active.append(directive)
        elif directive.startswith(("#else", "#elif")) and active:
            active[-1] = directive
        elif directive.startswith("#endif") and active:
            active.pop()
    return " | ".join(active)[:500] or None


def _classify_usage_in_tu(usage: CodeUsage, target_usrs: set[str], tu, ci, root: Path):
    simple = usage.symbol.rsplit("::", 1)[-1]
    candidate = _absolute(root.joinpath(*PurePosixPath(usage.file_path).parts))
    candidate_name = str(candidate)
    refs: dict[tuple[int, int], set[str]] = defaultdict(set)
    declarations: dict[tuple[int, int], str] = {}
    macro_definitions: dict[int, str] = {}
    macro_expansions: dict[str, list[int]] = defaultdict(list)
    target_ref_lines: set[int] = set()

    ref_kinds = {
        kind for name in (
            "DECL_REF_EXPR", "MEMBER_REF_EXPR", "OVERLOADED_DECL_REF",
            "TYPE_REF", "TEMPLATE_REF",
        ) if (kind := getattr(ci.CursorKind, name, None)) is not None
    }
    for cursor in tu.cursor.walk_preorder():
        filename = _file_name(cursor.location)
        if filename != candidate_name:
            continue
        pos = (cursor.location.line, cursor.location.column)
        spelling = getattr(cursor, "spelling", "")
        if cursor.kind in ref_kinds and spelling in {simple, usage.symbol}:
            usrs = _cursor_usrs(cursor, ci)
            refs[pos].update(usrs)
            if usrs & target_usrs:
                target_ref_lines.add(cursor.location.line)
        elif cursor.kind == getattr(ci.CursorKind, "USING_DECLARATION", None) and spelling == simple:
            for child in cursor.get_children():
                if child.kind in ref_kinds:
                    usrs = _cursor_usrs(child, ci)
                    refs[(child.location.line, child.location.column)].update(usrs)
                    if usrs & target_usrs:
                        target_ref_lines.add(child.location.line)
        elif getattr(cursor.kind, "is_declaration", lambda: False)() and spelling == simple:
            referenced = getattr(cursor, "referenced", None)
            usr = cursor.get_usr() if hasattr(cursor, "get_usr") else ""
            declarations[pos] = usr or (referenced.get_usr() if referenced else "")
        elif cursor.kind == getattr(ci.CursorKind, "MACRO_DEFINITION", None):
            macro_definitions[cursor.location.line] = spelling
        elif cursor.kind == getattr(ci.CursorKind, "MACRO_INSTANTIATION", None):
            macro_expansions[spelling].append(cursor.location.line)

    tokens = [
        token for token in tu.get_tokens(extent=tu.cursor.extent)
        if _file_name(token.location) == candidate_name and token.location.line == usage.line_start
        and (token.spelling == simple or simple in token.spelling)
    ]
    if not tokens:
        return None

    code_positions = {
        (token.location.line, token.location.column)
        for token in tokens
        if token.kind not in {ci.TokenKind.COMMENT, ci.TokenKind.LITERAL}
    }
    inert_only = not code_positions
    if inert_only:
        return "unaffected", "Clang token is inside a comment or literal", None, None

    macro_name = macro_definitions.get(usage.line_start)
    if macro_name:
        expansions = macro_expansions.get(macro_name, [])
        if any(line in target_ref_lines for line in expansions):
            matching = sorted(target_usrs)
            usr = matching[0] if len(matching) == 1 else None
            return "definite", f"macro {macro_name} expands to the affected declaration", usr, None
        return "possible", f"macro {macro_name} has no resolved affected expansion", None, None

    matched_refs = [refs[pos] for pos in code_positions if refs.get(pos)]
    if matched_refs:
        all_usrs = set().union(*matched_refs)
        affected = all_usrs & target_usrs
        if affected and all_usrs <= target_usrs:
            usr = sorted(affected)[0] if len(affected) == 1 else None
            target_cursor = next((
                cursor for cursor in tu.cursor.walk_preorder()
                if _file_name(cursor.location) == candidate_name
                and (cursor.location.line, cursor.location.column) in code_positions
                and _cursor_usrs(cursor, ci) & affected
            ), None)
            referenced = getattr(target_cursor, "referenced", None) if target_cursor else None
            declaration_file = _file_name(referenced.location) if referenced and getattr(referenced, "location", None) else None
            return "definite", f"reference resolves to affected USR {usr or sorted(affected)}", usr, declaration_file
        if target_usrs and not affected:
            return "unaffected", f"reference resolves to unrelated USR(s) {sorted(all_usrs)}", None, None
        return "possible", "AST references on this line are ambiguous", None, None

    declaration_positions = code_positions & set(declarations)
    if declaration_positions == code_positions:
        usr = next((declarations[position] for position in sorted(declaration_positions) if declarations[position]), None)
        return "unaffected", f"source declares a different entity ({usr or 'no USR'})", None, None
    return "possible", "identifier has no active reference node in this compile configuration", None, None


def _combine_observations(observations: list[tuple[str, str, str | None, str | None]]):
    verdicts = {item[0] for item in observations}
    if len(verdicts) != 1:
        return "possible", "compile configurations produced conflicting AST evidence", None, None
    verdict = observations[0][0]
    proofs = sorted({item[1] for item in observations})
    usrs = sorted({item[2] for item in observations if item[2]})
    files = sorted({item[3] for item in observations if item[3]})
    return verdict, "; ".join(proofs), usrs[0] if len(usrs) == 1 else None, files[0] if len(files) == 1 else None


def resolve_usages(
    *, worktree: Path, usages: list[CodeUsage], changes: list[BreakingChange], compile_commands_path: Path
) -> list[CodeUsage]:
    """Classify candidates using exact R1 USRs; unavailable AST support is a safe fallback."""
    if not usages or not compile_commands_path.is_file():
        return usages
    try:
        commands = load_compile_commands(compile_commands_path)
        ci = load_cindex()
    except (ClangUnavailable, OSError, ValueError) as exc:
        log.info("Clang AST analysis unavailable; retaining text candidates: %s", type(exc).__name__)
        return usages

    root = Path(worktree).resolve(strict=True)
    paths: dict[str, list[int]] = defaultdict(list)
    for index, usage in enumerate(usages):
        source = root.joinpath(*PurePosixPath(usage.file_path).parts).resolve(strict=False)
        paths[str(source)].append(index)
    observations: dict[int, list[tuple[str, str, str | None, str | None]]] = defaultdict(list)
    index = ci.Index.create()
    options = getattr(ci.TranslationUnit, "PARSE_DETAILED_PROCESSING_RECORD", 0)

    for entry in commands:
        try:
            source_file = _absolute(entry.file)
            try:
                source_file.relative_to(root)
            except ValueError:
                continue
            if not source_file.is_file():
                continue
            tu = index.parse(entry.file, args=_clang_args(entry), options=options)
            tokens_by_location: dict[tuple[str, int], bool] = {}
            for token in tu.get_tokens(extent=tu.cursor.extent):
                filename = _file_name(token.location)
                if filename is not None:
                    tokens_by_location[(filename, token.location.line)] = True
            for filename, usage_indexes in paths.items():
                if not any((filename, usages[i].line_start) in tokens_by_location for i in usage_indexes):
                    continue
                for usage_index in usage_indexes:
                    usage = usages[usage_index]
                    if (filename, usage.line_start) not in tokens_by_location:
                        continue
                    target_usrs = _usage_targets(usage, changes)
                    outcome = _classify_usage_in_tu(usage, target_usrs, tu, ci, root)
                    if outcome is not None:
                        observations[usage_index].append(outcome)
        except Exception as exc:
            log.debug("Clang failed to analyze a translation unit (%s)", type(exc).__name__)

    resolved: list[CodeUsage] = []
    for index, usage in enumerate(usages):
        evidence = observations.get(index)
        if not evidence:
            resolved.append(usage)
            continue
        verdict, proof, usr, declaration_file = _combine_observations(evidence)
        impact = ImpactLevel(verdict)
        data = usage.model_dump(mode="python")
        data.update(
            impact=impact,
            confidence=0.99 if impact == ImpactLevel.DEFINITE else (1.0 if impact == ImpactLevel.UNAFFECTED else usage.confidence),
            analysis_method=AnalysisMethod.CLANG_AST,
            resolved_by=ResolutionLayer.CLANG_AST,
            resolution_proof=proof,
            resolved_usr=usr,
            declaration_file=declaration_file,
            preprocessor_context=(
                _preprocessor_context(root.joinpath(*PurePosixPath(usage.file_path).parts), usage.line_start)
                if impact == ImpactLevel.POSSIBLE else usage.preprocessor_context
            ),
        )
        resolved.append(CodeUsage.model_validate(data))
    return resolved
