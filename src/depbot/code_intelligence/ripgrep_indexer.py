"""Deterministic, bounded ripgrep search for candidate C/C++ API usages."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

log = logging.getLogger(__name__)

SOURCE_GLOBS = ("*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx")
EXCLUDED_GLOBS = (
    "!.git/**", "!**/.git/**", "!build/**", "!**/build/**", "!build-*/**",
    "!**/build-*/**", "!cmake-build-*/**", "!**/cmake-build-*/**", "!.cache/**",
    "!**/.cache/**", "!vendor/**", "!**/vendor/**", "!generated/**",
    "!**/generated/**", "!third-party/**", "!**/third-party/**", "!third_party/**",
    "!**/third_party/**", "!**/node_modules/**", "!.venv/**", "!**/.venv/**",
    "!vcpkg_installed/**", "!**/vcpkg_installed/**", "!__pycache__/**",
    "!**/__pycache__/**", "!.pytest_cache/**", "!**/.pytest_cache/**",
    "!.mypy_cache/**", "!**/.mypy_cache/**", "!.ruff_cache/**", "!**/.ruff_cache/**",
    "!cache/**", "!**/cache/**", "!**/*.generated.*", "!**/*_generated.*", "!**/*.pb.*",
)
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000
_READ_CHUNK_BYTES = 64 * 1024


class RipgrepIndexError(RuntimeError):
    """Base exception for an unsuccessful or unsafe index operation."""


class RipgrepUnavailable(RipgrepIndexError):
    """The ripgrep executable could not be found."""


class RipgrepTimeout(RipgrepIndexError):
    """The ripgrep search exceeded its time limit."""


class RipgrepExecutionError(RipgrepIndexError):
    """Ripgrep returned an unexpected failure status."""


class RipgrepOutputError(RipgrepIndexError):
    """Ripgrep emitted malformed or oversized output."""


@dataclass(frozen=True)
class UsageMatch:
    symbol: str
    file_path: str
    line_number: int
    snippet: str


class SymbolLike(Protocol):
    """Minimal interface accepted for change-linked symbol candidates."""

    symbol: str


def _inside(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RipgrepOutputError(f"ripgrep returned unsafe path: {relative!r}")
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RipgrepOutputError(f"ripgrep path escapes worktree: {relative!r}") from exc
    return resolved


def _parse_output(root: Path, symbol: str, stdout: bytes, max_output_bytes: int) -> list[UsageMatch]:
    if len(stdout) > max_output_bytes:
        raise RipgrepOutputError("ripgrep output exceeded configured byte limit")
    matches: list[UsageMatch] = []
    try:
        for raw in stdout.splitlines():
            record = json.loads(raw)
            if record.get("type") != "match":
                continue
            data = record["data"]
            rel = data["path"]["text"].replace("\\", "/")
            path = _inside(root, rel)
            if not path.is_file():
                raise RipgrepOutputError(f"ripgrep returned a non-file path: {rel!r}")
            line = data["lines"]["text"].rstrip("\r\n")
            line_number = int(data["line_number"])
            if line_number < 1:
                raise ValueError("line number must be positive")
            matches.append(UsageMatch(symbol, path.relative_to(root).as_posix(), line_number, line[:1000]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RipgrepOutputError):
            raise
        raise RipgrepOutputError("malformed ripgrep JSON output") from exc
    return matches


def _run_bounded(
    args: list[str], root: Path, symbol: str, timeout: float, output_limit: int
) -> tuple[int, bytes, bytes]:
    """Drain subprocess pipes while retaining bounded output; terminate on overflow."""
    try:
        proc = subprocess.Popen(args, cwd=root, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RipgrepUnavailable(f"ripgrep executable not found: {args[0]}") from exc

    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def drain_stdout() -> None:
        assert proc.stdout is not None
        while chunk := proc.stdout.read(_READ_CHUNK_BYTES):
            remaining = output_limit - len(stdout)
            if len(chunk) > remaining:
                stdout.extend(chunk[:max(remaining, 0)])
                overflow.set()
                return
            stdout.extend(chunk)

    def drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := proc.stderr.read(_READ_CHUNK_BYTES):
            remaining = 4096 - len(stderr)
            if remaining > 0:
                stderr.extend(chunk[:remaining])

    out_thread = threading.Thread(target=drain_stdout, daemon=True)
    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    out_thread.start()
    err_thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if overflow.is_set():
            proc.terminate()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            proc.terminate()
            break
        time.sleep(0.01)
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    out_thread.join(timeout=0.5)
    err_thread.join(timeout=0.5)
    if overflow.is_set():
        raise RipgrepOutputError(f"ripgrep output exceeded {output_limit} byte limit; process terminated")
    if timed_out:
        raise RipgrepTimeout(f"ripgrep timed out searching for {symbol!r}")
    return int(proc.returncode or 0), bytes(stdout), bytes(stderr)


def search_symbols(
    worktree: Path,
    symbols: Sequence[str | SymbolLike],
    *,
    rg_executable: str = "rg",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> list[UsageMatch]:
    """Find literal symbol occurrences in source files under ``worktree``.

    Symlinks are not traversed by ripgrep's default behavior. Output paths are
    resolved and checked to remain inside the supplied worktree before return.
    """
    root = Path(worktree).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"worktree is not a directory: {root}")
    if timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ValueError("timeout_seconds and max_output_bytes must be positive")
    results: list[UsageMatch] = []
    normalized = (item if isinstance(item, str) else item.symbol for item in symbols)
    for symbol in dict.fromkeys(s for s in normalized if isinstance(s, str) and s):
        args = [rg_executable, "--json", "--fixed-strings", "--line-number", "--color", "never",
                "--max-columns", "1000", "--max-columns-preview"]
        for glob in SOURCE_GLOBS:
            args.extend(("--glob", glob))
        for glob in EXCLUDED_GLOBS:
            args.extend(("--glob", glob))
        args.extend(("--", symbol, "."))
        returncode, stdout, stderr = _run_bounded(args, root, symbol, timeout_seconds, max_output_bytes)
        if returncode == 1:
            continue
        if returncode != 0:
            raise RipgrepExecutionError(
                f"ripgrep failed for {symbol!r} with exit code {returncode}: "
                f"{stderr.decode('utf-8', errors='replace')}"
            )
        results.extend(_parse_output(root, symbol, stdout, max_output_bytes))
    results.sort(key=lambda item: (item.file_path, item.line_number, item.symbol, item.snippet))
    log.debug("ripgrep indexed %d candidate usages in %s", len(results), root)
    return results
