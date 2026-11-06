"""R1 — public API diff of a dependency between two versions (libclang; no LLM).

Written by Member A at the team's request; research/** is Member B's area, and B
wires this into ``research_release`` (see docs/CONTRACTS.md §10).

Pipeline:
1. Locate the dependency's headers for each version from the compile databases the
   engine already produced: baseline (old version) and probe (new version). Include
   directories that are identical on both sides belong to unchanged dependencies
   and are dropped; what remains is the upgraded dependency.
2. File-level diff first (relative path + SHA-256): only headers that changed, appeared
   or disappeared are parsed. Any moved declaration changes both files, so this is safe.
3. Parse each side's changed headers in one in-memory translation unit, with that
   side's include path and the project's ``-std``/``-D`` flags.
4. Extract public declarations located in those headers, keyed by USR (macros by name),
   and diff: removed, signature changed (same name, new parameter/return types),
   newly deprecated, macro body changed. Rename *candidates* are reported as hints
   only; replacement symbols are never invented here (R2/the changelog confirms them).
5. Safety: if the new side has parse errors, removal/signature claims are withheld
   (a broken parse would look like a mass removal) and the result is marked incomplete.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from depbot.clang_support import ClangUnavailable, load_cindex
from depbot.models import BreakingChange, ChangeSource, ChangeType
from depbot.repository.compile_db import CompileCommand, include_dirs, load_compile_commands

HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl"})
NON_PUBLIC_NAMESPACES = frozenset({"detail", "details", "internal", "impl", "__detail", "_impl"})
_VENDOR_DIRS = ("build", "vcpkg_installed")  # dependency headers installed inside a worktree


class ApiEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str  # USR, or "macro:NAME"
    kind: str
    name: str  # simple name (what text search and the changelog use)
    qualified_name: str
    signature: str
    deprecated: bool = False
    header: str  # path relative to its include directory
    file: str  # absolute path of the header
    line: int
    excerpt: str


class ApiDiffResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = True  # False when libclang / compile databases are missing
    complete: bool = True  # False when parse errors forced claims to be withheld
    reason: str | None = None
    old_include_dirs: list[str] = Field(default_factory=list)
    new_include_dirs: list[str] = Field(default_factory=list)
    changed_headers: list[str] = Field(default_factory=list)
    old_parse_errors: list[str] = Field(default_factory=list)
    new_parse_errors: list[str] = Field(default_factory=list)
    old_declarations: int = 0
    new_declarations: int = 0
    changes: list[BreakingChange] = Field(default_factory=list)


# ------------------------------------------------------------------ header discovery


def _headers(dirs: list[Path]) -> dict[str, Path]:
    """relative header path -> absolute path (first include directory wins, like the compiler)."""
    found: dict[str, Path] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in HEADER_SUFFIXES:
                found.setdefault(path.relative_to(base).as_posix(), path.resolve())
    return found


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changed_headers(old_dirs: list[Path], new_dirs: list[Path]) -> tuple[dict[str, Path], dict[str, Path]]:
    """Headers that differ between versions: (old side, new side), keyed by relative path."""
    old, new = _headers(old_dirs), _headers(new_dirs)
    old_changed = {rel: p for rel, p in old.items() if rel not in new or _sha(p) != _sha(new[rel])}
    new_changed = {rel: p for rel, p in new.items() if rel not in old or _sha(p) != _sha(old[rel])}
    return old_changed, new_changed


def dependency_include_dirs(entries: list[CompileCommand], worktree: Path) -> list[Path]:
    """Include dirs that are not project sources: outside the worktree, or installed
    into it by the build/package manager (``build/``, ``vcpkg_installed/``)."""
    root = worktree.resolve()
    result: dict[str, Path] = {}
    for entry in entries:
        for d in include_dirs(entry):
            path = Path(d).resolve()
            inside = path == root or root in path.parents
            if not inside or (path != root and path.relative_to(root).parts[0] in _VENDOR_DIRS):
                result.setdefault(str(path), path)
    return list(result.values())


def project_flags(entries: list[CompileCommand]) -> list[str]:
    """Language-standard and define flags of the project's first translation unit."""
    if not entries:
        return ["-std=c++17"]
    args = entries[0].arguments
    flags: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith(("-std=", "/std:")):
            flags.append(a if a.startswith("-") else "-std=" + a.split(":", 1)[1])
        elif a in ("-D", "-U") and i + 1 < len(args):
            flags += [a, args[i + 1]]
            i += 1
        elif a.startswith(("-D", "-U")) and len(a) > 2:
            flags.append(a)
        i += 1
    return flags if any(f.startswith("-std=") for f in flags) else ["-std=c++17", *flags]


# ------------------------------------------------------------------ declarations


@dataclass
class _Side:
    entries: dict[str, list[ApiEntry]] = field(default_factory=dict)  # key -> redeclarations
    errors: list[str] = field(default_factory=list)


def _qualified(cursor, ci) -> tuple[str, bool]:
    parts, public = [], True
    parent = cursor.semantic_parent
    while parent is not None and parent.kind != ci.CursorKind.TRANSLATION_UNIT:
        if parent.spelling:
            parts.append(parent.spelling)
            if parent.kind == ci.CursorKind.NAMESPACE and parent.spelling in NON_PUBLIC_NAMESPACES:
                public = False
        parent = parent.semantic_parent
    parts.reverse()
    return "::".join([*parts, cursor.spelling]), public


def _line_text(path: Path, line: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[line - 1].strip()
    except (OSError, IndexError):
        return ""


def _parse_side(ci, headers: dict[str, Path], dirs: list[Path], flags: list[str]) -> _Side:
    side = _Side()
    if not headers:
        return side
    wanted = {p: rel for rel, p in headers.items()}
    source = "".join(f'#include "{p}"\n' for p in sorted(headers.values()))
    args = ["-x", "c++", *flags, *[f"-I{d}" for d in dirs], "-fsyntax-only"]
    tu = ci.Index.create().parse(
        "__depbot_api__.cpp", args=args, unsaved_files=[("__depbot_api__.cpp", source)],
        options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD | ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES,
    )
    side.errors = [f"{d.location.file}:{d.location.line}: {d.spelling}" if d.location.file else d.spelling
                   for d in tu.diagnostics if d.severity >= ci.Diagnostic.Error]
    K = ci.CursorKind
    kinds = {K.FUNCTION_DECL: "function", K.CXX_METHOD: "method", K.CONSTRUCTOR: "constructor",
             K.FUNCTION_TEMPLATE: "function_template", K.CLASS_DECL: "class", K.STRUCT_DECL: "struct",
             K.CLASS_TEMPLATE: "class_template", K.ENUM_DECL: "enum", K.ENUM_CONSTANT_DECL: "enumerator",
             K.TYPEDEF_DECL: "typedef", K.TYPE_ALIAS_DECL: "alias", K.VAR_DECL: "variable",
             K.FIELD_DECL: "field", K.MACRO_DEFINITION: "macro"}
    private = {ci.AccessSpecifier.PRIVATE, ci.AccessSpecifier.PROTECTED}
    for cur in tu.cursor.walk_preorder():
        kind = kinds.get(cur.kind)
        if kind is None or cur.location.file is None or not cur.spelling:
            continue
        file = Path(cur.location.file.name).resolve()
        if file not in wanted or cur.spelling.startswith("__"):
            continue
        if kind == "macro":
            tokens = [t.spelling for t in cur.get_tokens()]
            if len(tokens) <= 1:  # include guards and empty markers
                continue
            body = " ".join(tokens[1:])
            entry = ApiEntry(key=f"macro:{cur.spelling}", kind=kind, name=cur.spelling,
                             qualified_name=cur.spelling, signature=f"#define {cur.spelling} {body}",
                             header=wanted[file], file=str(file), line=cur.location.line,
                             excerpt=_line_text(file, cur.location.line))
        else:
            if cur.access_specifier in private:
                continue
            if cur.semantic_parent is not None and cur.semantic_parent.kind in (
                K.FUNCTION_DECL, K.CXX_METHOD, K.CONSTRUCTOR, K.FUNCTION_TEMPLATE
            ):
                continue  # locals / parameters
            if kind in ("class", "struct", "enum") and not cur.is_definition():
                continue  # forward declarations
            qualified, public = _qualified(cur, ci)
            if not public:
                continue
            if kind in ("function", "method", "constructor", "function_template"):
                const = " const" if kind == "method" and cur.is_const_method() else ""
                ret = "" if kind == "constructor" else f"{cur.result_type.spelling} "
                params = cur.displayname[cur.displayname.find("("):] if "(" in cur.displayname else "()"
                signature = f"{ret}{qualified}{params}{const}"
            elif kind in ("typedef", "alias"):
                signature = f"{qualified} = {cur.underlying_typedef_type.spelling}"
            elif kind in ("variable", "field"):
                signature = f"{cur.type.spelling} {qualified}"
            elif kind == "enumerator":
                signature = f"{qualified} = {cur.enum_value}"
            else:
                signature = f"{kind} {qualified}"
            entry = ApiEntry(key=cur.get_usr() or f"{kind}:{qualified}", kind=kind, name=cur.spelling,
                             qualified_name=qualified, signature=signature,
                             deprecated=cur.availability == ci.AvailabilityKind.DEPRECATED,
                             header=wanted[file], file=str(file), line=cur.location.line,
                             excerpt=_line_text(file, cur.location.line))
        side.entries.setdefault(entry.key, []).append(entry)
    return side


# ------------------------------------------------------------------ diff


_LITERAL = re.compile(r'^(?:\(?\s*)?(?:0[xX][0-9a-fA-F]+|\d+[uUlL]*|"[^"]*")(?:\s*\)?)$')


def _is_version_macro(old: ApiEntry, new: ApiEntry) -> bool:
    """`#define FOO_VERSION 1` → `2` changes every release; it is not a breaking change."""
    def body(e: ApiEntry) -> str:
        return e.signature.split(" ", 2)[2] if e.signature.count(" ") >= 2 else ""
    return "VERSION" in old.name.upper() and bool(_LITERAL.match(body(old))) and bool(_LITERAL.match(body(new)))


def _change(n: int, dependency: str, version: str, **kw) -> BreakingChange:
    return BreakingChange(id=f"{dependency}-{version}-API{n}", source=ChangeSource.API_DIFF, **kw)


def diff_api(
    *, dependency: str, to_version: str, old_dirs: list[Path], new_dirs: list[Path],
    flags: list[str] | None = None,
) -> ApiDiffResult:
    """Diff the public API found under two sets of include directories."""
    result = ApiDiffResult(old_include_dirs=[str(d) for d in old_dirs], new_include_dirs=[str(d) for d in new_dirs])
    try:
        ci = load_cindex()
    except ClangUnavailable as exc:
        return result.model_copy(update={"available": False, "reason": str(exc)})
    old_h, new_h = changed_headers(old_dirs, new_dirs)
    result.changed_headers = sorted(set(old_h) | set(new_h))
    if not result.changed_headers:
        return result.model_copy(update={"reason": "no dependency header changed between versions"})
    flags = flags or ["-std=c++17"]
    old, new = _parse_side(ci, old_h, old_dirs, flags), _parse_side(ci, new_h, new_dirs, flags)
    result.old_parse_errors, result.new_parse_errors = old.errors[:20], new.errors[:20]
    result.old_declarations, result.new_declarations = len(old.entries), len(new.entries)
    withhold = bool(new.errors)

    new_by_name: dict[tuple[str, str], list[ApiEntry]] = {}
    for decls in new.entries.values():
        new_by_name.setdefault((decls[0].kind, decls[0].qualified_name), []).append(decls[0])

    changes: list[BreakingChange] = []
    for key, decls in sorted(old.entries.items(), key=lambda kv: (kv[1][0].qualified_name, kv[0])):
        o = decls[0]
        uri = Path(o.file).as_uri()
        if key in new.entries:
            n = new.entries[key][0]
            if o.kind == "macro" and o.signature != n.signature and not _is_version_macro(o, n):
                changes.append(_change(len(changes) + 1, dependency, to_version,
                    title=f"macro {o.name} changed", change_type=ChangeType.BEHAVIOR,
                    affected_symbols=[o.name], old_behavior=o.signature, new_behavior=n.signature,
                    migration_advice="Macro definition changed; review every expansion.",
                    evidence_urls=[uri, Path(n.file).as_uri()], evidence_excerpt=o.excerpt, confidence=0.6))
            elif o.kind != "macro" and o.signature != n.signature and not withhold:
                changes.append(_change(len(changes) + 1, dependency, to_version,
                    title=f"{o.qualified_name} signature changed", change_type=ChangeType.SIGNATURE,
                    affected_symbols=[o.name], affected_usrs=[key], old_behavior=o.signature,
                    new_behavior=n.signature, migration_advice=f"Update uses of {o.qualified_name} to: {n.signature}",
                    evidence_urls=[uri, Path(n.file).as_uri()], evidence_excerpt=o.excerpt, confidence=0.95))
            if n.deprecated and not o.deprecated:
                changes.append(_change(len(changes) + 1, dependency, to_version,
                    title=f"{o.qualified_name} is deprecated", change_type=ChangeType.DEPRECATED,
                    affected_symbols=[o.name], affected_usrs=[key] if o.kind != "macro" else [],
                    migration_advice=f"{o.qualified_name} is marked deprecated in {to_version}; check the "
                                     "release notes for its replacement.",
                    evidence_urls=[Path(n.file).as_uri()], evidence_excerpt=n.excerpt, confidence=0.9))
            continue
        if withhold:
            continue
        same_name = new_by_name.get((o.kind, o.qualified_name), [])
        if same_name:  # overload / parameter change: same name, different USR
            sigs = "; ".join(sorted(e.signature for e in same_name))
            changes.append(_change(len(changes) + 1, dependency, to_version,
                title=f"{o.qualified_name} signature changed", change_type=ChangeType.SIGNATURE,
                affected_symbols=[o.name], affected_usrs=[key], old_behavior=o.signature, new_behavior=sigs,
                migration_advice=f"{o.signature} no longer exists; available now: {sigs}",
                evidence_urls=[uri, Path(same_name[0].file).as_uri()], evidence_excerpt=o.excerpt, confidence=0.95))
            continue
        hints = sorted({e.qualified_name for k, (e, *_) in new.entries.items() if k not in old.entries
                        and e.kind == o.kind and e.signature.replace(e.qualified_name, "") ==
                        o.signature.replace(o.qualified_name, "")})
        advice = f"{o.qualified_name} was removed."
        if hints:
            advice += (f" New declarations with an identical signature: {', '.join(hints)} "
                       "(candidate replacement; confirm with the release notes).")
        changes.append(_change(len(changes) + 1, dependency, to_version,
            title=f"{o.qualified_name} was removed", change_type=ChangeType.REMOVED,
            affected_symbols=[o.name], affected_usrs=[key] if o.kind != "macro" else [],
            old_behavior=o.signature, migration_advice=advice,
            evidence_urls=[uri], evidence_excerpt=o.excerpt, confidence=0.99))

    reason = None
    if withhold:
        reason = "new-version headers did not parse cleanly; removal/signature claims withheld"
    return result.model_copy(update={"changes": changes, "complete": not withhold, "reason": reason})


def diff_dependency_api_for_job(*, artifacts_dir: Path, dependency: str, to_version: str) -> ApiDiffResult:
    """R1 for one repository job, from the engine's artifacts:
    ``compile_commands.baseline.json`` (old version, worktree) and
    ``probe/compile_commands.json`` (new version, probe worktree)."""
    base_db = artifacts_dir / "compile_commands.baseline.json"
    probe_db = artifacts_dir / "probe" / "compile_commands.json"
    missing = [p.name if p.parent == artifacts_dir else f"probe/{p.name}" for p in (base_db, probe_db) if not p.is_file()]
    if missing:
        return ApiDiffResult(available=False, reason=f"compile database(s) missing: {', '.join(missing)}")
    old_entries, new_entries = load_compile_commands(base_db), load_compile_commands(probe_db)
    old_dirs = dependency_include_dirs(old_entries, artifacts_dir / "worktree")
    new_dirs = dependency_include_dirs(new_entries, artifacts_dir / "probe" / "worktree")
    shared = {str(d) for d in old_dirs} & {str(d) for d in new_dirs}  # unchanged dependencies
    old_dirs = [d for d in old_dirs if str(d) not in shared]
    new_dirs = [d for d in new_dirs if str(d) not in shared]
    if not old_dirs or not new_dirs:
        return ApiDiffResult(available=False, reason="could not tell the upgraded dependency's include "
                             "directories apart from unchanged ones")
    return diff_api(dependency=dependency, to_version=to_version, old_dirs=old_dirs, new_dirs=new_dirs,
                    flags=project_flags(old_entries))


def confirm_replacements(api_changes: list[BreakingChange], changelog_changes: list[BreakingChange],
                         result: ApiDiffResult) -> list[BreakingChange]:
    """R1 x R2 cross-check: attach changelog replacement advice to an API removal only if the
    changelog names the same removed symbol and every replacement exists in the new API."""
    new_names = set()
    if result.available:
        # Names declared in the new version's changed headers are recorded as hints in advice;
        # a replacement must at least appear in the new headers' text to be accepted.
        new_text = "\n".join(Path(f).read_text(errors="replace") for f in _headers(
            [Path(d) for d in result.new_include_dirs]).values())
        new_names = set(re.findall(r"[A-Za-z_]\w*", new_text))
    by_symbol = {s: c for c in changelog_changes for s in c.affected_symbols if c.replacement_symbols}
    confirmed = []
    for change in api_changes:
        rule = next((by_symbol[s] for s in change.affected_symbols if s in by_symbol), None)
        if rule and change.change_type == ChangeType.REMOVED and all(r in new_names for r in rule.replacement_symbols):
            change = change.model_copy(update={
                "replacement_symbols": list(rule.replacement_symbols),
                "migration_advice": rule.migration_advice,
                "evidence_urls": [*change.evidence_urls, *rule.evidence_urls],
            })
        confirmed.append(change)
    return confirmed
