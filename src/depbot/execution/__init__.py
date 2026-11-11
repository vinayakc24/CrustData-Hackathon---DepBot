"""Pluggable execution of repository commands (install/configure/build/test).

The migration pipeline calls ``depbot.verification.command_runner.run_command``;
that function dispatches to the backend bound for the current job. Nothing in the
pipeline knows whether a command runs as a local process or in a container.
"""

from depbot.execution.backend import (
    CancelToken,
    ExecutionBackend,
    ExecutionConfig,
    ExecutionConfigError,
    ExecutionContext,
    ExecutionRequest,
    ResourceLimits,
    build_backend,
    current_context,
    use_execution,
)

__all__ = [
    "CancelToken",
    "ExecutionBackend",
    "ExecutionConfig",
    "ExecutionConfigError",
    "ExecutionContext",
    "ExecutionRequest",
    "ResourceLimits",
    "build_backend",
    "current_context",
    "use_execution",
]
