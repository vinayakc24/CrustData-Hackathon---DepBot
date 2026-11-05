"""Feasibility spike (Member A) for impact-cascade layer I2: libclang USR resolution.

Reference for Member B's ``code_intelligence/clang_analyzer.py``; not product code.
Classifies every textual match of a symbol as CONFIRMED / RULED_OUT / RESIDUAL
with a proof, using only the compiler front end (no LLM).

    pip install "clang==18.1.8"     # pure-Python bindings; uses the system libclang 18
    python docs/spikes/clang_usr_resolution/classify.py \
        --dep-include docs/spikes/clang_usr_resolution/dep/include \
        --source docs/spikes/clang_usr_resolution/proj/src/main.cpp --symbol old_sum

In the product, compile flags come from compile_commands.json (repo_facts["compile_commands"]).
"""

from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path

import clang.cindex as ci

LIBCLANG_CANDIDATES = ("/usr/lib/llvm-18/lib/libclang.so.1", "/usr/lib/x86_64-linux-gnu/libclang-18.so.1")
_REF_KINDS = {ci.CursorKind.DECL_REF_EXPR, ci.CursorKind.MEMBER_REF_EXPR, ci.CursorKind.OVERLOADED_DECL_REF,
              ci.CursorKind.TYPE_REF, ci.CursorKind.TEMPLATE_REF}


def _load() -> None:
    for path in LIBCLANG_CANDIDATES:
        if Path(path).is_file():
            ci.Config.set_library_file(path)
            break
    lib = ci.conf.lib
    # Not wrapped by the Python bindings in clang 18; needed for using-declarations.
    lib.clang_getNumOverloadedDecls.argtypes = [ci.Cursor]
    lib.clang_getNumOverloadedDecls.restype = ctypes.c_uint
    lib.clang_getOverloadedDecl.argtypes = [ci.Cursor, ctypes.c_uint]
    lib.clang_getOverloadedDecl.restype = ci.Cursor


def _usrs(cursor: ci.Cursor) -> set[str]:
    if cursor.kind == ci.CursorKind.OVERLOADED_DECL_REF:
        lib = ci.conf.lib
        n = lib.clang_getNumOverloadedDecls(cursor)
        return {lib.clang_getOverloadedDecl(cursor, i).get_usr() for i in range(n)} - {""}
    return {cursor.referenced.get_usr()} - {""} if cursor.referenced else set()


def classify(dep_include: Path, source: Path, symbol: str, flags: list[str]) -> dict:
    dep_include, source = dep_include.resolve(), source.resolve()
    tu = ci.Index.create().parse(
        str(source), args=["-x", "c++", "-std=c++17", f"-I{dep_include}", *flags],
        options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    errors = [d.spelling for d in tu.diagnostics if d.severity >= ci.Diagnostic.Error]

    def here(c: ci.Cursor) -> bool:
        return bool(c.location.file) and Path(c.location.file.name).resolve() == source

    def in_dep(c: ci.Cursor) -> bool:
        return bool(c.location.file) and Path(c.location.file.name).resolve().is_relative_to(dep_include)

    # The changed API = declarations of `symbol` that live in the dependency's headers.
    targets = {c.get_usr() for c in tu.cursor.walk_preorder()
               if c.spelling == symbol and c.kind.is_declaration() and in_dep(c)}

    refs: dict[tuple[int, int], set[str]] = {}
    decls: dict[tuple[int, int], str] = {}
    macro_defs: dict[int, str] = {}
    expansions: dict[str, list[int]] = {}
    for c in tu.cursor.walk_preorder():
        if not here(c):
            continue
        pos = (c.location.line, c.location.column)
        if c.kind in _REF_KINDS and c.spelling == symbol:
            refs.setdefault(pos, set()).update(_usrs(c))
        elif c.kind == ci.CursorKind.USING_DECLARATION and c.spelling == symbol:
            for child in c.get_children():
                if child.kind == ci.CursorKind.OVERLOADED_DECL_REF:
                    refs.setdefault((child.location.line, child.location.column), set()).update(_usrs(child))
        elif c.kind.is_declaration() and c.spelling == symbol:
            decls[pos] = c.get_usr()
        elif c.kind == ci.CursorKind.MACRO_DEFINITION:
            macro_defs[c.location.line] = c.spelling
        elif c.kind == ci.CursorKind.MACRO_INSTANTIATION and c.referenced is not None:
            expansions.setdefault(c.spelling, []).append(c.location.line)

    # Expression references produced by a macro expansion are reported at the expansion
    # site; the edit belongs in the macro definition.
    ref_lines = {line for (line, _), usrs in refs.items() if usrs & targets}
    results = []
    for tok in tu.get_tokens(extent=tu.cursor.extent):
        if symbol not in tok.spelling or not here(tok):
            continue
        line, col = tok.location.line, tok.location.column
        entry = {"line": line, "column": col, "token": tok.kind.name}
        if tok.kind == ci.TokenKind.COMMENT:
            entry.update(verdict="RULED_OUT", proof="inside a comment")
        elif tok.kind == ci.TokenKind.LITERAL:
            entry.update(verdict="RULED_OUT", proof="inside a string/char literal")
        elif (line, col) in refs:
            usrs = refs[(line, col)]
            if usrs & targets:
                entry.update(verdict="CONFIRMED", proof=f"references {sorted(usrs & targets)}")
            else:
                entry.update(verdict="RULED_OUT", proof=f"references a different entity {sorted(usrs)}")
        elif (line, col) in decls:
            entry.update(verdict="RULED_OUT", proof=f"declares a different entity {decls[(line, col)]}")
        elif line in macro_defs:
            name = macro_defs[line]
            used_at = [l for l in expansions.get(name, []) if l in ref_lines]
            if used_at:
                entry.update(verdict="CONFIRMED", proof=f"in macro {name}; expansions at lines {used_at} "
                                                        "resolve to the changed API (edit the macro body)")
            else:
                entry.update(verdict="RESIDUAL", proof=f"in macro {name} with no resolved expansion in this TU")
        else:
            entry.update(verdict="RESIDUAL", proof="identifier with no AST node: inactive preprocessor "
                                                   "branch or uncompiled code in this configuration")
        results.append(entry)
    return {"source": str(source), "symbol": symbol, "target_usrs": sorted(targets),
            "parse_errors": errors, "matches": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dep-include", required=True, type=Path)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("flags", nargs="*")
    a = ap.parse_args()
    _load()
    print(json.dumps(classify(a.dep_include, a.source, a.symbol, a.flags), indent=2))


if __name__ == "__main__":
    main()
