"""Populate an isolated Conan home with local recipes (offline demo setup).

Only operator-supplied recipe directories are built (``conan create`` executes
their conanfile.py), never recipes discovered inside repositories.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from depbot.package_managers.base import minimal_env

SETUP_TIMEOUT_SECONDS = 600
_TAIL = 2000


class SetupStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    args: list[str]
    exit_code: int | None
    timed_out: bool = False
    duration_seconds: float
    output_tail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SetupError(RuntimeError):
    pass


def _run(label: str, args: list[str], cwd: Path, env: dict[str, str], timeout: int) -> SetupStep:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout, check=False, shell=False,
        )
        output = (proc.stdout + proc.stderr)[-_TAIL:]
        code, timed_out = proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        output = str(exc)[-_TAIL:]
        code, timed_out = None, True
    except FileNotFoundError as exc:
        output, code, timed_out = str(exc), None, False
    return SetupStep(
        label=label, args=args, exit_code=code, timed_out=timed_out,
        duration_seconds=round(time.monotonic() - start, 3), output_tail=output,
    )


def prepare_local_packages(
    recipes: list[Path], conan_home: Path, *, build_type: str = "Release",
    timeout: int = SETUP_TIMEOUT_SECONDS,
) -> list[SetupStep]:
    """Detect a default profile and ``conan create`` each recipe into ``conan_home``.
    Stops at the first failing step; returns every step that ran."""
    for recipe in recipes:
        if not (recipe / "conanfile.py").is_file():
            raise SetupError(f"not a Conan recipe directory (no conanfile.py): {recipe}")
    conan_home = conan_home.resolve()
    conan_home.mkdir(parents=True, exist_ok=True)
    env = minimal_env({"CONAN_HOME": str(conan_home)})
    steps = [
        _run("profile", ["conan", "profile", "detect", "--exist-ok"], conan_home, env, timeout)
    ]
    # The isolated home is offline by design: local recipes only, no public remotes.
    if steps[-1].succeeded:
        steps.append(_run("disable remotes", ["conan", "remote", "disable", "*"], conan_home, env, timeout))
    for recipe in recipes:
        if not steps[-1].succeeded:
            break
        steps.append(
            _run(
                f"create {recipe.name}",
                ["conan", "create", ".", "--build=missing", "-s", f"build_type={build_type}"],
                recipe.resolve(), env, timeout,
            )
        )
    return steps
