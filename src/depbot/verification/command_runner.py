"""Bounded, shell-free command execution for repository verification.

``run_command`` is the single entry point the pipeline uses. It dispatches to the
execution backend bound for the current job (``depbot.execution``): a local
process by default, a disposable container when isolation is configured.
"""

from __future__ import annotations

from pathlib import Path

from depbot.execution.backend import ExecutionRequest, current_context
from depbot.execution.local import MAX_LOG_BYTES, TAIL_BYTES, is_install_step
from depbot.models import CommandResult

__all__ = ["MAX_LOG_BYTES", "TAIL_BYTES", "run_command"]


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
    context = current_context()
    request = ExecutionRequest(
        command_label=command_label,
        args=args,
        cwd=cwd,
        artifacts_dir=artifacts_dir,
        timeout_seconds=timeout_seconds,
        extra_env=extra_env,
    )
    if context.install_lock is not None and is_install_step(command_label):
        # Package caches are shared between concurrent jobs and are not safe for
        # concurrent writers; installs run one at a time, builds/tests in parallel.
        with context.install_lock:
            return context.backend.run(request, context)
    return context.backend.run(request, context)
