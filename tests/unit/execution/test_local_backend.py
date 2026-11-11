"""Local execution backend: environment allow-list, process-tree cleanup, cancellation."""

import os
import sys
import threading
import time

import pytest

from depbot.execution import CancelToken, ExecutionContext, use_execution
from depbot.execution.local import LocalBackend
from depbot.package_managers.base import minimal_env
from depbot.verification.command_runner import run_command

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


def _run(tmp_path, args, timeout=20, **kw):
    return run_command(command_label="t.step", args=args, cwd=tmp_path, artifacts_dir=tmp_path / "logs",
                       timeout_seconds=timeout, extra_env=minimal_env(), **kw)


def test_host_secrets_never_reach_the_command(tmp_path, monkeypatch):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "s3cret-value")
    result = _run(tmp_path, [sys.executable, "-c", "import os; print(sorted(os.environ))"])
    assert result.succeeded
    assert "TOKEN" not in result.stdout_tail and "KEY" not in result.stdout_tail


@posix_only
def test_timeout_kills_the_whole_process_tree(tmp_path):
    marker = tmp_path / "grandchild-survived"
    result = _run(tmp_path, ["sh", "-c", f"(sleep 2; touch {marker}) & sleep 30"], timeout=0.5)
    assert result.timed_out and result.exit_code is None and not result.succeeded
    assert result.duration_seconds < 10
    time.sleep(2.5)
    assert not marker.exists(), "a background child outlived the timed-out command"


@posix_only
def test_background_children_are_reaped_after_a_normal_exit(tmp_path):
    marker = tmp_path / "daemon-survived"
    result = _run(tmp_path, ["sh", "-c", f"(sleep 1; touch {marker}) >/dev/null 2>&1 & exit 0"])
    assert result.succeeded
    time.sleep(1.5)
    assert not marker.exists()


def test_cancellation_stops_a_running_command(tmp_path):
    token = CancelToken()
    threading.Timer(0.3, lambda: token.cancel("operator request")).start()
    with use_execution(ExecutionContext(backend=LocalBackend(), cancel=token)):
        result = _run(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
    assert result.exit_code is None and not result.timed_out and not result.succeeded
    assert "cancelled: operator request" in result.stderr_tail
    assert result.duration_seconds < 10


def test_cancelled_job_does_not_start_new_commands(tmp_path):
    token = CancelToken()
    token.cancel("stop")
    marker = tmp_path / "ran"
    with use_execution(ExecutionContext(backend=LocalBackend(), cancel=token)):
        result = _run(tmp_path, [sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    assert not result.succeeded and "not started" in result.stderr_tail and not marker.exists()


def test_cancel_token_probe_reads_a_durable_flag():
    flag = {"set": False}
    token = CancelToken(probe=lambda: flag["set"], poll_seconds=0)
    assert not token.is_set()
    flag["set"] = True
    assert token.is_set()
    broken = CancelToken(probe=lambda: 1 / 0, poll_seconds=0)
    assert not broken.is_set()  # a failing probe neither cancels nor raises


def test_install_steps_are_serialised_by_the_shared_cache_lock(tmp_path):
    lock = threading.Lock()
    active, peak = [0], [0]

    class Probe(LocalBackend):
        def run(self, request, context):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.15)
            active[0] -= 1
            return super().run(request, context)

    def job(index):
        with use_execution(ExecutionContext(backend=Probe(), install_lock=lock)):
            run_command(command_label="baseline.dependency_install", args=[sys.executable, "-c", "pass"],
                        cwd=tmp_path, artifacts_dir=tmp_path / f"logs{index}", timeout_seconds=20)

    threads = [threading.Thread(target=job, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1


def test_linked_event_cancels_immediately_without_waiting_for_the_poll_interval():
    shared = threading.Event()
    token = CancelToken(probe=lambda: False, poll_seconds=3600, linked=shared)
    assert not token.is_set()
    shared.set()
    assert token.is_set() and token.reason == "fleet run cancelled"
