"""Bounded, shell-free subprocess execution for repository verification."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from depbot.models import CommandResult
from depbot.package_managers.base import minimal_env

MAX_LOG_BYTES = 1_000_000
TAIL_BYTES = 8_000


def _drain(stream, log_path: Path) -> tuple[str, bool]:
    tail = bytearray()
    written = 0
    truncated = False
    with log_path.open("wb") as log:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            keep = max(0, min(len(chunk), MAX_LOG_BYTES - written))
            if keep:
                log.write(chunk[:keep])
                written += keep
            if keep < len(chunk):
                truncated = True
            tail.extend(chunk)
            if len(tail) > TAIL_BYTES:
                del tail[:-TAIL_BYTES]
    return tail.decode("utf-8", errors="replace"), truncated


def _safe_environment(extra_env: dict[str, str] | None) -> dict[str, str]:
    env = minimal_env(extra_env)
    # Windows process creation and native tool loading need these OS variables.
    for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run_command(
    *,
    command_label: str,
    args: list[str],
    cwd: Path,
    artifacts_dir: Path,
    timeout_seconds: float,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """Run an argv command with a timeout and capped log files/tails."""
    started = time.monotonic()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifacts_dir / f"{command_label.replace('.', '-')}.stdout.log"
    stderr_path = artifacts_dir / f"{command_label.replace('.', '-')}.stderr.log"
    if not args or any(not isinstance(arg, str) for arg in args):
        return CommandResult(
            command_label=command_label,
            args=[str(arg) for arg in args],
            cwd=str(cwd),
            exit_code=None,
            duration_seconds=0,
            stderr_tail="refusing an empty or non-string command argument list",
        )

    stdout_tail = ""
    stderr_tail = ""
    truncated = False
    timed_out = False
    exit_code: int | None = None
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=_safe_environment(extra_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        assert proc.stdout is not None and proc.stderr is not None
        results: dict[str, tuple[str, bool]] = {}
        out_thread = threading.Thread(
            target=lambda: results.__setitem__("stdout", _drain(proc.stdout, stdout_path)),
            daemon=True,
        )
        err_thread = threading.Thread(
            target=lambda: results.__setitem__("stderr", _drain(proc.stderr, stderr_path)),
            daemon=True,
        )
        out_thread.start()
        err_thread.start()
        try:
            exit_code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                exit_code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait()
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        if out_thread.is_alive() or err_thread.is_alive():
            proc.kill()
            out_thread.join(timeout=2)
            err_thread.join(timeout=2)
        stdout_tail, out_truncated = results.get("stdout", ("", False))
        stderr_tail, err_truncated = results.get("stderr", ("", False))
        truncated = out_truncated or err_truncated
    except FileNotFoundError as exc:
        stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        stderr_tail = f"{type(exc).__name__}: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:
        stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        stderr_tail = f"{type(exc).__name__}: {exc}"

    return CommandResult(
        command_label=command_label,
        args=args,
        cwd=str(cwd.resolve()),
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - started, 3),
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        output_truncated=truncated,
    )
