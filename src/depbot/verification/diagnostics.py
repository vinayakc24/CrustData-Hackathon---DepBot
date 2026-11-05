"""Structured compiler/CMake/linker diagnostics from build logs (contract §9, item C4).

Reads the *full* command logs (not the bounded tails), recognises GCC/Clang,
MSVC, CMake and GNU-ld messages, and returns ``CompilerDiagnostic`` records with
repository-relative paths. Paths outside the worktree (dependency headers, the
package cache, build directories' generated files stay inside) become ``None``.
"""

from __future__ import annotations

import re
from pathlib import Path

from depbot.models import CommandResult, CompilerDiagnostic

MAX_LOG_BYTES = 4_000_000
MAX_DIAGNOSTICS = 200
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_GCC = re.compile(
    r"^(?P<file>[^\s:][^:\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s+"
    r"(?P<sev>fatal error|error|warning|note):\s+(?P<msg>.*?)(?:\s+\[(?P<opt>-W[^\]]+)\])?\s*$"
)
_MSVC = re.compile(
    r"^(?P<file>[^\n(]+?)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s+"
    r"(?P<sev>fatal error|error|warning)\s+(?P<code>[A-Z]+\d+)\s*:\s+(?P<msg>.*?)\s*$"
)
_CMAKE = re.compile(r"^CMake (?P<sev>Error|Warning)(?: \(dev\))? at (?P<file>[^:\n]+):(?P<line>\d+)\s*(?P<ctx>\(.*\))?:?\s*$")
_LD = re.compile(r"^(?:\S*/)?(?:ld|ld\.\w+|lld|ld64)(?:\.exe)?: (?P<msg>.*(?:undefined reference|undefined symbol|multiple definition).*)$")


def _relative(raw: str, worktree: Path, build_dirs: list[Path]) -> str | None:
    path = Path(raw.strip())
    root = worktree.resolve()
    candidates = [path] if path.is_absolute() else [root / path, *(d / path for d in build_dirs)]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == root or root in resolved.parents:
            return resolved.relative_to(root).as_posix()
    return None


def parse_diagnostics(text: str, *, worktree: Path, command_label: str,
                      build_dirs: list[Path] | None = None) -> list[CompilerDiagnostic]:
    build_dirs = build_dirs or []
    found: list[CompilerDiagnostic] = []
    lines = _ANSI.sub("", text).splitlines()
    for index, line in enumerate(lines):
        if m := _GCC.match(line):
            found.append(CompilerDiagnostic(
                file_path=_relative(m["file"], worktree, build_dirs), line=int(m["line"]),
                column=int(m["col"]) if m["col"] else None, severity=m["sev"], message=m["msg"].strip(),
                option=m["opt"], command_label=command_label))
        elif m := _MSVC.match(line):
            found.append(CompilerDiagnostic(
                file_path=_relative(m["file"], worktree, build_dirs), line=int(m["line"]),
                column=int(m["col"]) if m["col"] else None, severity=m["sev"],
                message=f"{m['code']}: {m['msg'].strip()}", command_label=command_label))
        elif m := _CMAKE.match(line):
            detail = next((l.strip() for l in lines[index + 1:index + 4] if l.strip()), "")
            found.append(CompilerDiagnostic(
                file_path=_relative(m["file"], worktree, build_dirs), line=int(m["line"]),
                severity=m["sev"].lower(), message=f"{(m['ctx'] or '').strip()} {detail}".strip(),
                command_label=command_label))
        elif m := _LD.match(line):
            found.append(CompilerDiagnostic(file_path=None, severity="error", message=m["msg"].strip(),
                                            command_label=command_label))
    unique: dict[tuple, CompilerDiagnostic] = {}
    for d in found:  # parallel make prints duplicates
        unique.setdefault((d.file_path, d.line, d.column, d.severity, d.message), d)
    return list(unique.values())[:MAX_DIAGNOSTICS]


def _read(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def diagnostics_for(results: list[CommandResult | None], *, worktree: Path,
                    build_dirs: list[Path] | None = None) -> list[CompilerDiagnostic]:
    """Diagnostics from configure/build/test steps, using full logs (tails as fallback)."""
    collected: list[CompilerDiagnostic] = []
    for result in results:
        if result is None:
            continue
        text = (_read(result.stdout_path) or result.stdout_tail) + "\n" + (_read(result.stderr_path) or result.stderr_tail)
        collected += parse_diagnostics(text, worktree=worktree, command_label=result.command_label,
                                       build_dirs=build_dirs)
    return collected[:MAX_DIAGNOSTICS]
