"""Bounded worker pool for repository jobs.

A fixed number of worker threads pull from a bounded queue; ``submit`` blocks when
the queue is full (back-pressure instead of unbounded memory). A handler that
raises fails only its own item. ``cancel`` stops workers from starting queued
items. No distributed infrastructure: one process, a handful of threads.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from depbot.execution import ResourceLimits

log = logging.getLogger(__name__)

T = TypeVar("T")
MAX_WORKERS = 16
_STOP = object()


class ResourceBudget(BaseModel):
    """What the host may spend on concurrent jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: float = Field(gt=0)
    memory_mb: int = Field(gt=0)

    @classmethod
    def detect(cls) -> ResourceBudget:
        cpus = float(os.cpu_count() or 1)
        try:
            memory_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
        except (ValueError, OSError, AttributeError):
            memory_mb = 4096
        return cls(cpus=cpus, memory_mb=max(int(memory_mb), 1))


def effective_workers(requested: int, limits: ResourceLimits, budget: ResourceBudget | None = None) -> int:
    """Workers the host can afford: each job may use up to ``limits`` at once.
    Never below 1 (a fleet must make progress) and never above the request."""
    if requested < 1:
        raise ValueError("workers must be >= 1")
    budget = budget or ResourceBudget.detect()
    by_cpu = int(budget.cpus // limits.cpus)
    by_memory = int(budget.memory_mb // limits.memory_mb)
    return max(1, min(requested, MAX_WORKERS, by_cpu, by_memory))


class WorkerPool(Generic[T]):
    def __init__(self, handler: Callable[[T, str], None], *, workers: int = 1,
                 queue_size: int | None = None, name: str = "depbot") -> None:
        if workers < 1 or workers > MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
        self._handler = handler
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size or workers * 2)
        self._cancelled = threading.Event()
        self.errors: list[tuple[T, BaseException]] = []
        self.skipped: list[T] = []  # items never started because the pool was cancelled
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._work, args=(f"{name}-w{i + 1}",), name=f"{name}-w{i + 1}", daemon=True)
            for i in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    @property
    def workers(self) -> int:
        return len(self._threads)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def submit(self, item: T) -> None:
        """Queue one item; blocks while the queue is full."""
        self._queue.put(item)

    def cancel(self) -> None:
        self._cancelled.set()

    def _work(self, worker_id: str) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                if self._cancelled.is_set():
                    with self._lock:
                        self.skipped.append(item)
                    continue
                try:
                    self._handler(item, worker_id)
                except BaseException as exc:  # isolate: even a dying handler never stops the pool
                    log.exception("worker %s: handler failed for %r", worker_id, item)
                    with self._lock:
                        self.errors.append((item, exc))
            finally:
                self._queue.task_done()

    def close(self) -> None:
        """Wait for every queued item, then stop the workers."""
        for _ in self._threads:
            self._queue.put(_STOP)
        for thread in self._threads:
            thread.join()

    def __enter__(self) -> WorkerPool[T]:
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        if exc_type is not None:
            self.cancel()
        self.close()
