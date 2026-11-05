"""Compilation database helpers (owner: Member A).

Every configure exports ``compile_commands.json`` (``CMAKE_EXPORT_COMPILE_COMMANDS``
via ``adapter.command_env()``). The engine copies the baseline database to
``<artifacts_dir>/compile_commands.baseline.json`` and records it in
``state.repo_facts["compile_commands"]`` so the impact stage can parse each
translation unit with the repository's real flags.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class CompileCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str  # absolute source path
    directory: str
    arguments: list[str]


def find_compile_commands(worktree: Path) -> Path | None:
    """Newest ``compile_commands.json`` under ``<worktree>/build`` (CMake binary dirs)."""
    build = worktree / "build"
    if not build.is_dir():
        return None
    found = sorted(build.rglob("compile_commands.json"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def load_compile_commands(path: Path) -> list[CompileCommand]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} is not a JSON array")
    entries = []
    for item in raw:
        args = item.get("arguments") or shlex.split(item.get("command", ""))
        directory = item.get("directory", "")
        file = item.get("file", "")
        if file and not Path(file).is_absolute():
            file = str((Path(directory) / file).resolve())
        entries.append(CompileCommand(file=file, directory=directory, arguments=args))
    return entries


def include_dirs(entry: CompileCommand) -> list[str]:
    """``-I`` / ``-isystem`` directories of one entry, in order (absolute when possible)."""
    dirs: list[str] = []
    args = entry.arguments
    i = 0
    while i < len(args):
        arg = args[i]
        for flag in ("-isystem", "-I"):
            if arg == flag and i + 1 < len(args):
                dirs.append(args[i + 1])
                i += 1
                break
            if arg.startswith(flag) and len(arg) > len(flag):
                dirs.append(arg[len(flag):])
                break
        i += 1
    base = Path(entry.directory)
    return [str((base / d).resolve()) if not Path(d).is_absolute() else d for d in dirs]


def external_include_dirs(entries: list[CompileCommand], worktree: Path) -> list[str]:
    """Include directories outside the worktree: dependency headers (e.g. the Conan
    package cache). The AST layer treats declarations under these as dependency API."""
    root = worktree.resolve()
    seen: dict[str, None] = {}
    for entry in entries:
        for d in include_dirs(entry):
            path = Path(d).resolve()
            if path != root and root not in path.parents:
                seen.setdefault(str(path), None)
    return list(seen)
