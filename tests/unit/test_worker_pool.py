"""Bounded worker pool: concurrency limit, back-pressure, failure isolation, cancellation."""

import threading
import time

import pytest

from depbot.execution import ResourceLimits
from depbot.fleet.pool import ResourceBudget, WorkerPool, effective_workers


def test_concurrency_never_exceeds_the_worker_count():
    active, peak, done, lock = [0], [0], [], threading.Lock()

    def handler(item, worker):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
            done.append(item)

    with WorkerPool(handler, workers=3) as pool:
        for i in range(12):
            pool.submit(i)
    assert sorted(done) == list(range(12)) and 2 <= peak[0] <= 3


def test_submit_blocks_when_the_queue_is_full():
    release = threading.Event()
    pool = WorkerPool(lambda item, worker: release.wait(5), workers=1, queue_size=1)
    pool.submit("running")
    time.sleep(0.1)  # the worker has taken it
    pool.submit("queued")
    blocked = threading.Thread(target=pool.submit, args=("overflow",), daemon=True)
    blocked.start()
    blocked.join(timeout=0.3)
    assert blocked.is_alive(), "submit returned although the bounded queue was full"
    release.set()
    blocked.join(timeout=5)
    assert not blocked.is_alive()
    pool.close()


def test_a_failing_item_does_not_stop_the_others():
    done = []

    class Fatal(BaseException):
        pass

    def handler(item, worker):
        if item == 1:
            raise RuntimeError("boom")
        if item == 2:
            raise Fatal("worker died")
        done.append(item)

    with WorkerPool(handler, workers=2) as pool:
        for i in range(6):
            pool.submit(i)
    assert sorted(done) == [0, 3, 4, 5]
    assert sorted(item for item, _ in pool.errors) == [1, 2]


def test_cancel_skips_items_that_have_not_started():
    started, gate = [], threading.Event()

    def handler(item, worker):
        started.append(item)
        gate.wait(5)

    pool = WorkerPool(handler, workers=1, queue_size=10)
    for i in range(4):
        pool.submit(i)
    time.sleep(0.1)
    pool.cancel()
    gate.set()
    pool.close()
    assert started == [0] and sorted(pool.skipped) == [1, 2, 3] and pool.cancelled


def test_worker_count_is_capped_by_the_resource_budget():
    limits = ResourceLimits(cpus=2, memory_mb=4096)
    assert effective_workers(8, limits, ResourceBudget(cpus=16, memory_mb=64_000)) == 8
    assert effective_workers(8, limits, ResourceBudget(cpus=4, memory_mb=64_000)) == 2  # CPU-bound
    assert effective_workers(8, limits, ResourceBudget(cpus=16, memory_mb=9000)) == 2  # memory-bound
    assert effective_workers(8, limits, ResourceBudget(cpus=1, memory_mb=512)) == 1  # always progress
    assert effective_workers(2, limits, ResourceBudget(cpus=64, memory_mb=640_000)) == 2
    with pytest.raises(ValueError):
        effective_workers(0, limits)
    with pytest.raises(ValueError):
        WorkerPool(lambda i, w: None, workers=0)
