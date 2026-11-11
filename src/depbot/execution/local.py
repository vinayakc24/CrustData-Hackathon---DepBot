"""Local-process execution and the bounded process supervisor shared by all backends."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from depbot.execution.backend import ExecutionBackend, ExecutionContext, ExecutionRequest
from depbot.models import CommandResult
from depbot.package_managers.base import minimal_env

MAX_LOG_BYTES = 1_000_000
TAIL_BYTES = 8_000
_POLL_SECONDS = 0.1
_GRACE_SECONDS = 2.0
INSTALL_LABEL_SUFFIX = "dependency_install"


def is_install_step(label: str) -> bool:
    return label.endswith(INSTALL_LABEL_SUFFIX)


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


def safe_environment(extra_env: dict[str, str] | None) -> dict[str, str]:
    env = minimal_env(extra_env)
    # Windows process creation and native tool loading need these OS variables.
    for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _signal_tree(proc: subprocess.Popen, sig: int) -> None:
    """Signal the whole process group so grandchildren (compilers, tests) die too."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _stop(proc: subprocess.Popen) -> None:
    _signal_tree(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # Always follow with KILL: a child may have ignored TERM after the leader exited.
    _signal_tree(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        proc.wait(timeout=_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def supervise(
    request: ExecutionRequest,
    context: ExecutionContext,
    *,
    argv: list[str],
    env: dict[str, str],
    spawn_cwd: Path,
    on_stop: Callable[[], None] | None = None,
) -> CommandResult:
    """Run ``argv`` with a deadline, cancellation and capped logs.

    ``request.args``/``request.cwd`` are what is recorded (the logical command);
    ``argv``/``spawn_cwd`` are what is actually spawned (they differ for container
    backends). ``on_stop`` runs after a timeout or cancellation, before the process
    group is killed (e.g. to kill a container by name).
    """
    started = time.monotonic()
    request.artifacts_dir.mkdir(parents=True, exist_ok=True)
    stem = request.command_label.replace(".", "-")
    stdout_path = request.artifacts_dir / f"{stem}.stdout.log"
    stderr_path = request.artifacts_dir / f"{stem}.stderr.log"

    def result(**fields) -> CommandResult:
        return CommandResult(
            command_label=request.command_label,
            args=[str(a) for a in request.args],
            cwd=str(request.cwd.resolve()),
            duration_seconds=round(time.monotonic() - started, 3),
            **fields,
        )

    if not request.args or any(not isinstance(arg, str) for arg in request.args):
        return CommandResult(
            command_label=request.command_label,
            args=[str(arg) for arg in request.args],
            cwd=str(request.cwd),
            exit_code=None,
            duration_seconds=0,
            stderr_tail="refusing an empty or non-string command argument list",
        )
    if context.cancel.is_set():
        stderr_path.write_text(f"not started: {context.cancel.reason}\n", encoding="utf-8")
        return result(exit_code=None, stderr_path=str(stderr_path.resolve()),
                      stderr_tail=f"not started: {context.cancel.reason}")

    stdout_tail = stderr_tail = ""
    truncated = timed_out = cancelled = False
    exit_code: int | None = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=spawn_cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
        assert proc.stdout is not None and proc.stderr is not None
        results: dict[str, tuple[str, bool]] = {}
        threads = [
            threading.Thread(
                target=lambda: results.__setitem__("stdout", _drain(proc.stdout, stdout_path)), daemon=True
            ),
            threading.Thread(
                target=lambda: results.__setitem__("stderr", _drain(proc.stderr, stderr_path)), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()
        deadline = started + request.timeout_seconds
        try:
            while True:
                try:
                    exit_code = proc.wait(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if context.cancel.is_set():
                    cancelled = True
                elif time.monotonic() >= deadline:
                    timed_out = True
                if cancelled or timed_out:
                    break
        finally:
            if cancelled or timed_out or proc.poll() is None:
                if on_stop is not None:
                    try:
                        on_stop()
                    except Exception:
                        pass
                _stop(proc)
                # The command did not finish on its own; its termination code is not a result.
                exit_code = None
            elif os.name == "posix":
                # The leader exited; reap anything it left running in its group.
                _signal_tree(proc, signal.SIGKILL)
        for thread in threads:
            thread.join(timeout=5)
        stdout_tail, out_truncated = results.get("stdout", ("", False))
        stderr_tail, err_truncated = results.get("stderr", ("", False))
        truncated = out_truncated or err_truncated
        if cancelled:
            stderr_tail = (stderr_tail + f"\n[depbot] command cancelled: {context.cancel.reason}").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        stderr_tail = f"{type(exc).__name__}: {exc}"

    return result(
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        output_truncated=truncated,
    )


class LocalBackend(ExecutionBackend):
    """Runs commands as host processes with an allow-listed environment.

    No filesystem, network or resource isolation: use it for development and for
    repositories you trust. Untrusted repositories need a container backend.
    """

    name = "local"

    def run(self, request: ExecutionRequest, context: ExecutionContext) -> CommandResult:
        return supervise(
            request,
            context,
            argv=list(request.args),
            env=safe_environment(request.extra_env),
            spawn_cwd=request.cwd,
        )
