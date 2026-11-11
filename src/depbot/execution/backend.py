"""Execution backend contract, configuration and the per-job execution context."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from depbot.models import CommandResult


class ExecutionConfigError(ValueError):
    """The execution configuration is unsafe or incomplete."""


class ResourceLimits(BaseModel):
    """Per-command ceilings. Enforced by container backends; the local backend
    enforces only the time limit (it is for development on trusted repositories)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: float = Field(default=2.0, gt=0, le=64)
    memory_mb: int = Field(default=4096, ge=64)
    pids: int = Field(default=512, ge=16)
    tmpfs_mb: int = Field(default=1024, ge=16)


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["local", "docker", "podman"] = "local"
    image: str | None = None  # required for container backends; pin by digest in production
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    network: Literal["none", "bridge"] = "none"
    # Dependency installation may need a package registry; everything else stays offline.
    network_for_install: bool = False
    # Extra host directories visible inside the container (absolute paths).
    read_only_paths: list[str] = Field(default_factory=list)
    read_write_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> ExecutionConfig:
        if self.backend != "local" and not self.image:
            raise ValueError(f"the {self.backend} backend requires an image")
        for raw in (*self.read_only_paths, *self.read_write_paths):
            if not Path(raw).is_absolute():
                raise ValueError(f"mount path must be absolute: {raw}")
        return self


@dataclass(frozen=True)
class ExecutionRequest:
    command_label: str
    args: list[str]
    cwd: Path
    artifacts_dir: Path
    timeout_seconds: float
    extra_env: dict[str, str] | None = None


class CancelToken:
    """Cooperative cancellation. Set in-process with ``cancel()`` or through a shared
    ``linked`` event (checked on every call); ``probe`` lets a durable flag (e.g. a
    SQLite row set by another process) cancel the job too, polled at most every
    ``poll_seconds``."""

    def __init__(self, probe: Callable[[], bool] | None = None, poll_seconds: float = 1.0,
                 linked: threading.Event | None = None) -> None:
        self._event = threading.Event()
        self._linked = linked
        self._probe = probe
        self._poll_seconds = poll_seconds
        self._last_probe = 0.0
        self.reason = "cancelled"

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self._linked is not None and self._linked.is_set():
            self.cancel("fleet run cancelled")
            return True
        if self._probe is not None and time.monotonic() - self._last_probe >= self._poll_seconds:
            self._last_probe = time.monotonic()
            try:
                if self._probe():
                    self.cancel("cancellation requested")
            except Exception:  # a broken probe must not cancel or crash the job
                pass
        return self._event.is_set()


class ExecutionBackend(ABC):
    name: str

    @abstractmethod
    def run(self, request: ExecutionRequest, context: ExecutionContext) -> CommandResult:
        """Run one argv command. Must never raise for command failure, timeout or
        cancellation: those are reported in the returned ``CommandResult``."""

    def problems(self) -> list[str]:
        """Reasons this backend cannot run commands here (checked before any job)."""
        return []

    def describe(self) -> dict[str, str | None]:
        """Identity of the execution environment, recorded in run telemetry."""
        return {"backend": self.name, "image": None, "image_digest": None}


@dataclass
class ExecutionContext:
    """Everything a backend needs that is specific to one repository job."""

    backend: ExecutionBackend
    # The job's private directory (working copy, probe copy, logs). The only
    # location a container backend mounts read-write by default.
    workspace: Path | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    # Serialises steps that mutate a cache shared between jobs (package installs).
    install_lock: threading.Lock | None = None


_current: ContextVar[ExecutionContext | None] = ContextVar("depbot_execution", default=None)


def current_context() -> ExecutionContext:
    """The bound context, or a local-process default for callers outside a job."""
    context = _current.get()
    if context is None:
        from depbot.execution.local import LocalBackend

        context = ExecutionContext(backend=LocalBackend())
    return context


@contextmanager
def use_execution(context: ExecutionContext) -> Iterator[ExecutionContext]:
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def build_backend(config: ExecutionConfig) -> ExecutionBackend:
    from depbot.execution.container import ContainerBackend
    from depbot.execution.local import LocalBackend

    if config.backend == "local":
        return LocalBackend()
    return ContainerBackend(config)
