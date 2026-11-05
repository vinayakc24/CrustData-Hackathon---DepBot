"""Impact layer I2: resolve textual candidates with libclang (no LLM).

Each translation unit of the baseline compile database is parsed with the project's
own flags. Every reference to a candidate symbol in a project file (``.cpp`` and
headers) is resolved to the declaration it names (USR). A candidate line is then:

* CONFIRMED  — references, re-declares or (via a macro body) expands to a changed
  dependency declaration;
* RULED_OUT  — refers to a different entity, or the match is in a comment/literal;
* RESIDUAL   — no AST node in this configuration (inactive ``#if`` branch,
  uninstantiated code); handed to the compiler probe / LLM / humans.

Changed declarations are identified by ``BreakingChange.affected_usrs`` (from R1) when
available, otherwise as declarations of that name inside the dependency's include
directories. Files no translation unit covers are reported as not covered.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from depbot.clang_support import ClangUnavailable, load_cindex, overloaded_decls
from depbot.repository.compile_db import CompileCommand, load_compile_commands
from depbot.research.header_diff import dependency_include_dirs

log = logging.getLogger(__name__)

MAX_TRANSLATION_UNITS = 200
_IF_OPEN = re.compile(r"^\s*#\s*(if|ifdef|ifndef)\b(.*)$")
_IF_ELSE = re.compile(r"^\s*#\s*(elif|else)\b(.*)$")
_IF_END = re.compile(r"^\s*#\s*endif\b")
_DROP_FLAGS = {"-c", "-MD", "-MMD", "-MP"}
_DROP_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ"}


@dataclass(frozen=True)
class Verdict:
    verdict: str  # "confirmed" | "ruled_out" | "residual"
    proof: str
    usr: str | None = None
    declaration_file: str | None = None
    preprocessor_context: str | None = None


@dataclass
class AstAnalysis:
    available: bool
    reason: str | None = None
    covered_files: set[str] = field(default_factory=set)  # repo-relative files seen by some TU
    parse_errors: list[str] = field(default_factory=list)
    translation_units: int = 0
    # (rel_path, line, symbol) -> Verdict, for the requested candidates only
    verdicts: dict[tuple[str, int, str], Verdict] = field(default_factory=dict)


@dataclass
class _Facts:
    refs: dict[tuple[str, int, str], set[tuple[str, str | None]]] = field(default_factory=dict)  # -> {(usr, decl_file)}
    decls: dict[tuple[str, int, str], set[str]] = field(default_factory=dict)
    token_kinds: dict[tuple[str, int, str], set[str]] = field(default_factory=dict)
    macro_defs: dict[tuple[str, int], str] = field(default_factory=dict)
    expansions: dict[str, set[tuple[str, int]]] = field(default_factory=dict)
    targets: dict[str, set[str]] = field(default_factory=dict)  # symbol -> target USRs found in dependency headers


def _tu_args(entry: CompileCommand) -> list[str]:
    args, skip = [], False
    source = Path(entry.file).name
    for arg in entry.arguments[1:]:
        if skip:
            skip = False
            continue
        if arg in _DROP_WITH_VALUE:
            skip = True
            continue
        if arg in _DROP_FLAGS:
            continue
        if arg == entry.file or Path(arg).name == source and not arg.startswith("-"):
            continue
        args.append(arg)
    return args


def _rel(path: str, root: Path) -> str | None:
    p = Path(path).resolve()
    if p == root or root not in p.parents:
        return None
    rel = p.relative_to(root)
    return None if rel.parts[0] in ("build", "vcpkg_installed") else rel.as_posix()


def _collect(ci, tu, root: Path, symbols: set[str], dep_dirs: list[Path], facts: _Facts) -> set[str]:
    K = ci.CursorKind
    ref_kinds = {K.DECL_REF_EXPR, K.MEMBER_REF_EXPR, K.OVERLOADED_DECL_REF, K.TYPE_REF, K.TEMPLATE_REF}
    covered: set[str] = set()

    def in_dep(path: str) -> bool:
        p = Path(path).resolve()
        return any(p == d or d in p.parents for d in dep_dirs)

    for cur in tu.cursor.walk_preorder():
        loc = cur.location
        if loc.file is None:
            continue
        fname = loc.file.name
        if cur.spelling in symbols and cur.kind.is_declaration() and in_dep(fname):
            facts.targets.setdefault(cur.spelling, set()).add(cur.get_usr())
            continue
        rel = _rel(fname, root)
        if rel is None:
            continue
        covered.add(rel)
        key = (rel, loc.line, cur.spelling)
        if cur.kind in ref_kinds and cur.spelling in symbols:
            decls = overloaded_decls(cur) if cur.kind == K.OVERLOADED_DECL_REF else [cur.referenced]
            for d in decls:
                if d is not None and d.get_usr():
                    dfile = d.location.file.name if d.location.file else None
                    facts.refs.setdefault(key, set()).add((d.get_usr(), dfile))
        elif cur.kind == K.USING_DECLARATION and cur.spelling in symbols:
            for child in cur.get_children():
                if child.kind == K.OVERLOADED_DECL_REF:
                    for d in overloaded_decls(child):
                        dfile = d.location.file.name if d.location.file else None
                        facts.refs.setdefault(key, set()).add((d.get_usr(), dfile))
        elif cur.kind == K.MACRO_DEFINITION:
            facts.macro_defs[(rel, loc.line)] = cur.spelling
        elif cur.kind == K.MACRO_INSTANTIATION:
            facts.expansions.setdefault(cur.spelling, set()).add((rel, loc.line))
        elif cur.kind.is_declaration() and cur.spelling in symbols:
            facts.decls.setdefault(key, set()).add(cur.get_usr())
    # Token kinds per covered project file (comments and literals rule matches out).
    for rel in covered:
        path = root / rel
        try:
            size = path.stat().st_size
            file = tu.get_file(str(path))
            extent = ci.SourceRange.from_locations(ci.SourceLocation.from_offset(tu, file, 0),
                                                   ci.SourceLocation.from_offset(tu, file, size))
        except Exception:
            continue
        for tok in tu.get_tokens(extent=extent):
            for sym in symbols:
                if sym in tok.spelling:
                    facts.token_kinds.setdefault((rel, tok.location.line, sym), set()).add(tok.kind.name)
    return covered


def _preprocessor_context(path: Path, line: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[: line - 1]
    except OSError:
        return None
    stack: list[str] = []
    for text in lines:
        if m := _IF_OPEN.match(text):
            stack.append(f"#{m[1]}{m[2]}".strip())
        elif (m := _IF_ELSE.match(text)) and stack:
            stack[-1] = f"{stack[-1]} / #{m[1]}{m[2]}".strip()
        elif _IF_END.match(text) and stack:
            stack.pop()
    return " > ".join(stack) or None


def analyze_candidates(
    *, worktree: Path, compile_db: Path, candidates: list[tuple[str, int, str]],
    target_usrs: dict[str, set[str]] | None = None,
) -> AstAnalysis:
    """Verdicts for ``(repo-relative file, line, symbol)`` candidates."""
    try:
        ci = load_cindex()
    except ClangUnavailable as exc:
        return AstAnalysis(available=False, reason=str(exc))
    if not compile_db.is_file():
        return AstAnalysis(available=False, reason=f"no compile database at {compile_db}")
    try:
        entries = load_compile_commands(compile_db)
    except (ValueError, OSError) as exc:
        return AstAnalysis(available=False, reason=f"unreadable compile database: {exc}")
    root = worktree.resolve()
    entries = [e for e in entries if _rel(e.file, root) is not None][:MAX_TRANSLATION_UNITS]
    if not entries:
        return AstAnalysis(available=False, reason="compile database has no project translation units")
    symbols = {c[2] for c in candidates}
    dep_dirs = [d.resolve() for d in dependency_include_dirs(entries, root)]
    facts, result = _Facts(), AstAnalysis(available=True)
    index = ci.Index.create()
    for entry in entries:
        tu = index.parse(entry.file, args=_tu_args(entry),
                         options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        result.translation_units += 1
        result.parse_errors += [f"{d.location.file}:{d.location.line}: {d.spelling}"
                                for d in tu.diagnostics if d.severity >= ci.Diagnostic.Error][:5]
        result.covered_files |= _collect(ci, tu, root, symbols, dep_dirs, facts)
    targets = {s: set(facts.targets.get(s, set())) | set((target_usrs or {}).get(s, set())) for s in symbols}

    for rel, line, sym in candidates:
        if rel not in result.covered_files:
            continue
        key, tgt = (rel, line, sym), targets.get(sym, set())
        refs = facts.refs.get(key, set())
        hit = sorted(u for u, _ in refs if u in tgt)
        if hit:
            dfile = next((f for u, f in refs if u == hit[0]), None)
            result.verdicts[key] = Verdict("confirmed", f"references {hit[0]}", hit[0], dfile)
        elif refs:
            other = sorted(u for u, _ in refs)
            result.verdicts[key] = Verdict("ruled_out", f"references a different entity {other}")
        elif facts.decls.get(key):
            usrs = facts.decls[key]
            if usrs & tgt:
                usr = sorted(usrs & tgt)[0]
                result.verdicts[key] = Verdict("confirmed", f"re-declares the changed API {usr}", usr)
            else:
                result.verdicts[key] = Verdict("ruled_out", f"declares a different entity {sorted(usrs)}")
        elif (rel, line) in facts.macro_defs:
            name = facts.macro_defs[(rel, line)]
            target_lines = {(r, l) for (r, l, _s), rs in facts.refs.items() if {u for u, _ in rs} & tgt}
            used = [l for (r, l) in facts.expansions.get(name, set()) if (r, l) in target_lines]
            if used:
                result.verdicts[key] = Verdict("confirmed", f"body of macro {name}; expansions at lines "
                                               f"{sorted(set(used))} resolve to the changed API (edit the macro)")
            else:
                result.verdicts[key] = Verdict("residual", f"in macro {name} with no resolved expansion",
                                               preprocessor_context=_preprocessor_context(root / rel, line))
        elif (kinds := facts.token_kinds.get(key)) and kinds <= {"COMMENT", "LITERAL"}:
            where = "a comment" if kinds == {"COMMENT"} else "a string/char literal"
            result.verdicts[key] = Verdict("ruled_out", f"inside {where}")
        else:
            result.verdicts[key] = Verdict(
                "residual", "no AST node in this build configuration (inactive preprocessor branch or "
                            "uncompiled code)", preprocessor_context=_preprocessor_context(root / rel, line))
    return result
