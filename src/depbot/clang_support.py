"""Shared libclang loading (owner: Member A; used by research R1 and B's AST impact layer).

    pip install -e ".[ast]"     # clang==18.1.8 Python bindings; uses the system libclang

``load_cindex()`` returns ``clang.cindex`` configured with a libclang shared library
(``$LIBCLANG_PATH``, then common LLVM 18 locations, then the linker search path), and
registers the C functions the clang 18 bindings do not wrap. It raises
``ClangUnavailable`` with an explanation instead of failing at import time, so
callers can fall back to text-based behaviour.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from functools import lru_cache
from pathlib import Path

LIBCLANG_ENV = "LIBCLANG_PATH"
_CANDIDATES = (
    "/usr/lib/llvm-18/lib/libclang.so.1",
    "/usr/lib/x86_64-linux-gnu/libclang-18.so.1",
    "/usr/lib/aarch64-linux-gnu/libclang-18.so.1",
    "/opt/homebrew/opt/llvm@18/lib/libclang.dylib",
    "/usr/local/opt/llvm@18/lib/libclang.dylib",
    "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
)


class ClangUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_cindex():
    try:
        import clang.cindex as ci
    except ImportError as exc:
        raise ClangUnavailable('libclang Python bindings missing: pip install "clang==18.1.8"') from exc
    if not ci.Config.loaded:
        library = os.environ.get(LIBCLANG_ENV) or next((p for p in _CANDIDATES if Path(p).is_file()), None)
        library = library or ctypes.util.find_library("clang") or ctypes.util.find_library("clang-18")
        if not library:
            raise ClangUnavailable(f"libclang shared library not found; set {LIBCLANG_ENV}")
        try:
            ci.Config.set_library_file(library)
        except Exception as exc:  # already-set or unusable library
            raise ClangUnavailable(f"cannot use libclang at {library}: {exc}") from exc
    try:
        lib = ci.conf.lib
        lib.clang_getNumOverloadedDecls.argtypes = [ci.Cursor]
        lib.clang_getNumOverloadedDecls.restype = ctypes.c_uint
        lib.clang_getOverloadedDecl.argtypes = [ci.Cursor, ctypes.c_uint]
        lib.clang_getOverloadedDecl.restype = ci.Cursor
        ci.Index.create()
    except Exception as exc:
        raise ClangUnavailable(f"libclang failed to load: {exc}") from exc
    return ci


def overloaded_decls(cursor) -> list:
    """Declarations behind an OVERLOADED_DECL_REF (using-declarations, overload sets)."""
    lib = load_cindex().conf.lib
    return [lib.clang_getOverloadedDecl(cursor, i) for i in range(lib.clang_getNumOverloadedDecls(cursor))]
