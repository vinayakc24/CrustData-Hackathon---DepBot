"""Disposable, hardened Docker/Podman execution for untrusted repositories.

Every command runs in a fresh container that is removed afterwards:

* non-root user, all capabilities dropped, ``no-new-privileges``;
* CPU, memory (no swap), PID and time limits;
* read-only root filesystem with a size-capped ``/tmp``;
* only the job's private workspace mounted read-write, plus explicitly
  configured paths; host credentials, the home directory and container-runtime
  sockets can never be mounted;
* no network unless configured (optionally only for dependency installation);
* only an allow-listed environment — no host ``PATH``/``HOME``, tokens or keys.

Host paths are mounted at identical paths so recorded commands, compile
databases and diagnostics stay valid on the host.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from depbot.execution.backend import (
    ExecutionBackend,
    ExecutionConfig,
    ExecutionConfigError,
    ExecutionContext,
    ExecutionRequest,
)
from depbot.execution.local import is_install_step, supervise
from depbot.models import CommandResult

# Host-specific variables that must not override the image's own environment.
_HOST_ONLY_ENV = frozenset({"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR"})
_CONTAINER_HOME = "/tmp/depbot-home"
_SYSTEM_DIRS = ("/etc", "/proc", "/sys", "/dev", "/run", "/var/run", "/private/var/run", "/boot", "/root")
_CREDENTIAL_DIRS = (".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube", ".config", ".netrc",
                    ".git-credentials", ".conan2", ".npmrc", ".pypirc")
_RUNTIME_TIMEOUT_SECONDS = 30
_UNPRIVILEGED_ID = 65534


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def validate_mount(raw: str | Path) -> Path:
    """Resolve a host path and refuse anything that would expose host secrets or control."""
    path = Path(raw).resolve()
    if not path.is_absolute() or path == Path(path.anchor):
        raise ExecutionConfigError(f"refusing to mount {path}: filesystem root")
    home = Path.home().resolve()
    if _is_within(home, path):
        raise ExecutionConfigError(f"refusing to mount {path}: it contains the home directory")
    for name in _CREDENTIAL_DIRS:
        secret = home / name
        if _is_within(path, secret):
            raise ExecutionConfigError(f"refusing to mount {path}: host credentials ({secret})")
    for system in _SYSTEM_DIRS:
        if _is_within(path, Path(system)) or _is_within(path, Path(system).resolve()):
            raise ExecutionConfigError(f"refusing to mount {path}: host system directory")
    if path.name.endswith(".sock"):
        raise ExecutionConfigError(f"refusing to mount {path}: socket")
    try:
        if stat.S_ISSOCK(path.stat().st_mode):
            raise ExecutionConfigError(f"refusing to mount {path}: socket")
    except OSError:
        pass
    return path


class ContainerBackend(ExecutionBackend):
    def __init__(self, config: ExecutionConfig, *, runtime: str | None = None) -> None:
        if config.backend == "local" or not config.image:
            raise ExecutionConfigError("ContainerBackend needs a docker/podman config with an image")
        self.config = config
        self.name = config.backend
        self.runtime = runtime or config.backend  # executable; overridable for tests
        self._read_only = [validate_mount(p) for p in config.read_only_paths]
        self._read_write = [validate_mount(p) for p in config.read_write_paths]
        self._digest: str | None = None

    # -- environment identity -------------------------------------------------

    def _runtime(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run([self.runtime, *args], capture_output=True, text=True, shell=False,
                                  stdin=subprocess.DEVNULL, timeout=_RUNTIME_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    def problems(self) -> list[str]:
        if shutil.which(self.runtime) is None:
            return [f"container runtime not found on PATH: {self.runtime}"]
        if self.image_digest() is None:
            return [f"image {self.config.image!r} is not available locally "
                    f"(pull it first: {self.runtime} pull {self.config.image})"]
        return []

    def image_digest(self) -> str | None:
        if self._digest is None:
            proc = self._runtime("image", "inspect", "--format", "{{.Id}}", self.config.image or "")
            if proc is not None and proc.returncode == 0 and proc.stdout.strip():
                self._digest = proc.stdout.strip().splitlines()[0]
        return self._digest

    def describe(self) -> dict[str, str | None]:
        return {"backend": self.name, "image": self.config.image, "image_digest": self.image_digest()}

    # -- command construction -------------------------------------------------

    def _mounts(self, context: ExecutionContext) -> tuple[list[Path], list[Path]]:
        read_write = list(self._read_write)
        if context.workspace is not None:
            read_write.insert(0, validate_mount(context.workspace))
        return self._read_only, read_write

    def build_argv(self, request: ExecutionRequest, context: ExecutionContext, name: str) -> list[str]:
        """The full ``<runtime> run ...`` argv. Raises ExecutionConfigError when unsafe."""
        read_only, read_write = self._mounts(context)
        cwd = request.cwd.resolve()
        if not any(_is_within(cwd, mount) for mount in (*read_write, *read_only)):
            raise ExecutionConfigError(f"working directory {cwd} is outside every mounted path")
        limits = self.config.limits
        uid = os.getuid() if hasattr(os, "getuid") else _UNPRIVILEGED_ID
        gid = os.getgid() if hasattr(os, "getgid") else _UNPRIVILEGED_ID
        if uid == 0:  # never run repository code as root, even if DepBot itself is
            uid = gid = _UNPRIVILEGED_ID
        network = self.config.network
        if network == "none" and self.config.network_for_install and is_install_step(request.command_label):
            network = "bridge"
        argv = [
            self.runtime, "run", "--rm", "--init", "--name", name,
            "--user", f"{uid}:{gid}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(limits.pids),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--cpus", f"{limits.cpus:g}",
            "--network", network,
            "--read-only",
            "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={limits.tmpfs_mb}m,mode=1777",
        ]
        if self.name == "podman":
            argv += ["--userns", "keep-id"]  # rootless: keep bind-mount ownership usable
        for mount in read_write:
            argv += ["--mount", f"type=bind,source={mount},target={mount}"]
        for mount in read_only:
            argv += ["--mount", f"type=bind,source={mount},target={mount},readonly"]
        env = {k: v for k, v in (request.extra_env or {}).items() if k not in _HOST_ONLY_ENV}
        env["HOME"] = _CONTAINER_HOME
        for key in sorted(env):
            argv += ["--env", f"{key}={env[key]}"]
        argv += ["--workdir", str(cwd), self.config.image or "", *request.args]
        return argv

    # -- execution ---------------------------------------------------------------

    def run(self, request: ExecutionRequest, context: ExecutionContext) -> CommandResult:
        name = f"depbot-{secrets.token_hex(8)}"
        try:
            argv = self.build_argv(request, context, name)
        except ExecutionConfigError as exc:
            return CommandResult(
                command_label=request.command_label, args=[str(a) for a in request.args],
                cwd=str(request.cwd), exit_code=None, duration_seconds=0,
                stderr_tail=f"refused by execution policy: {exc}",
            )
        # The runtime client needs only its own PATH/HOME; repository code never sees them.
        client_env = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST", "XDG_RUNTIME_DIR",
                                                 "CONTAINER_HOST") if k in os.environ}
        try:
            return supervise(
                request, context, argv=argv, env=client_env, spawn_cwd=request.artifacts_dir,
                on_stop=lambda: self._runtime("kill", name),
            )
        finally:
            # Idempotent: a no-op when ``--rm`` already removed it, a guarantee when the
            # client was killed (timeout, cancellation, crash) before it could.
            self._runtime("rm", "--force", "--volumes", name)
