"""Container backend: hardening flags, mount policy and cleanup.

A recording stand-in for the docker CLI checks the exact command DepBot builds and
the kill/remove calls it makes. Tests marked ``container`` run adversarial
workloads in a real runtime and are skipped when none is installed.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from depbot.execution import CancelToken, ExecutionConfig, ExecutionConfigError, ExecutionContext, ExecutionRequest
from depbot.execution.container import ContainerBackend, validate_mount

IMAGE = "test-image:1"

FAKE_RUNTIME = """#!{python}
import json, os, subprocess, sys
log = {log!r}
args = sys.argv[1:]
with open(log, "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:2] == ["image", "inspect"]:
    print("sha256:" + "ab" * 32)
    sys.exit(0)
if args[0] == "run":
    workdir = args[args.index("--workdir") + 1]
    command = args[args.index({image!r}) + 1:]
    sys.exit(subprocess.call(command, cwd=workdir))
sys.exit(0)
"""


@pytest.fixture
def runtime(tmp_path):
    log = tmp_path / "runtime-calls.jsonl"
    exe = tmp_path / "bin" / "docker"
    exe.parent.mkdir()
    exe.write_text(FAKE_RUNTIME.format(python=sys.executable, log=str(log), image=IMAGE))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return str(exe), calls


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "job"
    (ws / "worktree").mkdir(parents=True)
    return ws


def _backend(runtime_path, **config):
    return ContainerBackend(ExecutionConfig(backend="docker", image=IMAGE, **config), runtime=runtime_path)


def _request(workspace, args, label="upgraded.build", timeout=20, env=None):
    return ExecutionRequest(command_label=label, args=args, cwd=workspace / "worktree",
                            artifacts_dir=workspace / "logs", timeout_seconds=timeout, extra_env=env)


def _flag(argv, name):
    return [argv[i + 1] for i, a in enumerate(argv) if a == name]


def test_command_is_hardened_and_only_the_workspace_is_writable(runtime, workspace, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")
    backend = _backend(runtime[0])
    env = {"PATH": "/host/bin", "HOME": "/Users/someone", "CONAN_HOME": "/cache/conan", "CMAKE_EXPORT_COMPILE_COMMANDS": "ON"}
    argv = backend.build_argv(_request(workspace, ["cmake", "--build", "."], env=env),
                              ExecutionContext(backend=backend, workspace=workspace), "depbot-x")
    assert argv[:3] == [runtime[0], "run", "--rm"]
    assert _flag(argv, "--cap-drop") == ["ALL"]
    assert _flag(argv, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in argv and "--privileged" not in argv
    assert _flag(argv, "--network") == ["none"]
    assert _flag(argv, "--pids-limit") == ["512"] and _flag(argv, "--cpus") == ["2"]
    assert _flag(argv, "--memory") == ["4096m"] == _flag(argv, "--memory-swap")
    user = _flag(argv, "--user")[0]
    assert user.split(":")[0] != "0"
    ws = str(workspace.resolve())
    assert _flag(argv, "--mount") == [f"type=bind,source={ws},target={ws}"]
    envs = _flag(argv, "--env")
    assert "CONAN_HOME=/cache/conan" in envs and "HOME=/tmp/depbot-home" in envs
    assert not any(e.startswith(("PATH=", "GITHUB_TOKEN=")) or "someone" in e for e in envs)
    assert "s3cret" not in " ".join(argv) and "docker.sock" not in " ".join(argv)
    assert argv[-4:] == [IMAGE, "cmake", "--build", "."]


def test_root_host_user_is_mapped_to_an_unprivileged_container_user(runtime, workspace, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    backend = _backend(runtime[0])
    argv = backend.build_argv(_request(workspace, ["true"]), ExecutionContext(backend=backend, workspace=workspace), "n")
    assert _flag(argv, "--user") == ["65534:65534"]


def test_network_is_opened_only_for_dependency_install_when_configured(runtime, workspace):
    ctx = lambda b: ExecutionContext(backend=b, workspace=workspace)  # noqa: E731
    closed = _backend(runtime[0])
    assert _flag(closed.build_argv(_request(workspace, ["x"], label="baseline.dependency_install"), ctx(closed), "n"),
                 "--network") == ["none"]
    opened = _backend(runtime[0], network_for_install=True)
    assert _flag(opened.build_argv(_request(workspace, ["x"], label="baseline.dependency_install"), ctx(opened), "n"),
                 "--network") == ["bridge"]
    assert _flag(opened.build_argv(_request(workspace, ["x"], label="upgraded.tests"), ctx(opened), "n"),
                 "--network") == ["none"]


@pytest.mark.parametrize("path", [
    "/", str(Path.home()), str(Path.home().parent), str(Path.home() / ".ssh"), str(Path.home() / ".ssh" / "keys"),
    str(Path.home() / ".aws"), str(Path.home() / ".config" / "gh"), str(Path.home() / ".docker"),
    "/var/run/docker.sock", "/run/podman/podman.sock", "/etc", "/proc/1", "/var/run",
])
def test_host_credentials_sockets_and_system_paths_cannot_be_mounted(path):
    with pytest.raises(ExecutionConfigError):
        validate_mount(path)
    with pytest.raises(ExecutionConfigError):
        ContainerBackend(ExecutionConfig(backend="docker", image=IMAGE, read_write_paths=[path]))


def test_container_config_requires_an_image_and_absolute_mounts():
    with pytest.raises(ValueError):
        ExecutionConfig(backend="docker")
    with pytest.raises(ValueError):
        ExecutionConfig(backend="podman", image=IMAGE, read_only_paths=["relative/path"])


def test_working_directory_outside_the_mounts_is_refused_without_running(runtime, workspace, tmp_path):
    backend = _backend(runtime[0])
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    request = ExecutionRequest(command_label="x.build", args=["true"], cwd=outside,
                               artifacts_dir=workspace / "logs", timeout_seconds=5)
    result = backend.run(request, ExecutionContext(backend=backend, workspace=workspace))
    assert not result.succeeded and "refused by execution policy" in result.stderr_tail
    assert not any(call[0] == "run" for call in runtime[1]())


def test_unsafe_workspace_is_refused(runtime):
    backend = _backend(runtime[0])
    result = backend.run(
        ExecutionRequest(command_label="x.build", args=["true"], cwd=Path.home(), artifacts_dir=Path.home(),
                         timeout_seconds=5),
        ExecutionContext(backend=backend, workspace=Path.home()),
    )
    assert not result.succeeded and "home directory" in result.stderr_tail


def _names(calls, verb):
    return [call[-1] for call in calls if call[0] == verb]


def test_successful_command_runs_and_container_is_removed(runtime, workspace):
    backend = _backend(runtime[0])
    result = backend.run(_request(workspace, [sys.executable, "-c", "print('built')"]),
                         ExecutionContext(backend=backend, workspace=workspace))
    assert result.succeeded and "built" in result.stdout_tail
    assert result.args[0] == sys.executable  # the logical command is recorded, not the wrapper
    calls = runtime[1]()
    run = next(call for call in calls if call[0] == "run")
    name = run[run.index("--name") + 1]
    assert _names(calls, "rm") == [name] and not _names(calls, "kill")


def test_failed_command_reports_its_exit_code_and_is_removed(runtime, workspace):
    backend = _backend(runtime[0])
    result = backend.run(_request(workspace, [sys.executable, "-c", "raise SystemExit(7)"]),
                         ExecutionContext(backend=backend, workspace=workspace))
    assert result.exit_code == 7 and len(_names(runtime[1](), "rm")) == 1


def test_timeout_kills_and_removes_the_container(runtime, workspace):
    backend = _backend(runtime[0])
    result = backend.run(_request(workspace, [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5),
                         ExecutionContext(backend=backend, workspace=workspace))
    assert result.timed_out and result.exit_code is None
    calls = runtime[1]()
    run = next(call for call in calls if call[0] == "run")
    name = run[run.index("--name") + 1]
    assert _names(calls, "kill") == [name] and _names(calls, "rm") == [name]
    verbs = [call[0] for call in calls]
    assert verbs.index("kill") < verbs.index("rm")


def test_cancellation_kills_and_removes_the_container(runtime, workspace):
    backend = _backend(runtime[0])
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    result = backend.run(_request(workspace, [sys.executable, "-c", "import time; time.sleep(60)"], timeout=60),
                         ExecutionContext(backend=backend, workspace=workspace, cancel=token))
    assert not result.succeeded and not result.timed_out
    calls = runtime[1]()
    assert len(_names(calls, "kill")) == 1 and len(_names(calls, "rm")) == 1


def test_missing_runtime_is_a_reported_problem_not_a_crash(workspace):
    backend = ContainerBackend(ExecutionConfig(backend="docker", image=IMAGE), runtime="depbot-no-such-runtime")
    assert "not found" in backend.problems()[0]
    result = backend.run(_request(workspace, ["true"]), ExecutionContext(backend=backend, workspace=workspace))
    assert not result.succeeded and "FileNotFoundError" in result.stderr_tail


def test_image_digest_is_recorded_for_telemetry(runtime):
    backend = _backend(runtime[0])
    assert backend.problems() == []
    assert backend.describe() == {"backend": "docker", "image": IMAGE, "image_digest": "sha256:" + "ab" * 32}


# ------------------------------------------------------------- real runtime (adversarial)

REAL_IMAGE = os.environ.get("DEPBOT_TEST_IMAGE", "alpine:3.20")
REAL_RUNTIME = next((r for r in ("docker", "podman") if shutil.which(r)), None)


def _real_available():
    if REAL_RUNTIME is None:
        return False
    try:
        return subprocess.run([REAL_RUNTIME, "image", "inspect", REAL_IMAGE], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


real = pytest.mark.skipif(not _real_available(), reason=f"needs docker/podman with image {REAL_IMAGE} pulled")


def _real(workspace, script, **config):
    backend = ContainerBackend(ExecutionConfig(backend=REAL_RUNTIME, image=REAL_IMAGE, **config))
    result = backend.run(_request(workspace, ["sh", "-c", script], timeout=60),
                         ExecutionContext(backend=backend, workspace=workspace))
    return result


@real
@pytest.mark.container
def test_real_container_is_non_root_offline_and_cannot_write_outside_workspace(workspace):
    script = ("id -u; touch /escape 2>&1 || echo ROOTFS_READONLY; touch ./inside && echo WORKSPACE_OK; "
              "ls /var/run/docker.sock 2>&1 || echo NO_SOCKET; "
              "wget -q -T 3 -O- http://example.com >/dev/null 2>&1 || echo NO_NETWORK; "
              "ls -d /Users /home/* 2>/dev/null | head -1; echo END")
    result = _real(workspace, script)
    out = result.stdout_tail
    assert result.succeeded, result.stderr_tail
    assert out.splitlines()[0] != "0"
    for marker in ("ROOTFS_READONLY", "WORKSPACE_OK", "NO_SOCKET", "NO_NETWORK"):
        assert marker in out
    assert (workspace / "worktree" / "inside").exists()


@real
@pytest.mark.container
def test_real_container_enforces_pid_and_memory_limits(workspace):
    from depbot.execution import ResourceLimits

    bomb = _real(workspace, "for i in $(seq 1 200); do sleep 30 & done; wait",
                 limits=ResourceLimits(pids=32, memory_mb=128))
    assert not bomb.succeeded
    hog = _real(workspace, "tail -c 400m /dev/zero | cat >/dev/null; head -c 300000000 /dev/zero | tr '\\0' 'x' | "
                "awk '{a[NR]=$0} END{print length(a)}'", limits=ResourceLimits(memory_mb=64))
    assert not hog.succeeded
