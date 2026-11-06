"""Package-manager adapter interface (owner: Member A).

The orchestrator and Member C's verification code depend only on this interface.
Adapters never execute commands themselves: they return argument lists that the
verification command runner executes with timeouts (shell=False).
"""

from __future__ import annotations

import difflib
import os
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from depbot.models import ManifestChange, PackageManagerName


class PackageManagerError(RuntimeError):
    """Base class: every subclass is a safe, explained refusal."""


class DependencyNotFound(PackageManagerError):
    pass


class AmbiguousDependency(PackageManagerError):
    pass


class UnsupportedManifest(PackageManagerError):
    pass


class DeclaredDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    manifest_path: str  # repository-relative
    package_manager: PackageManagerName
    is_direct: bool = True
    section: str | None = None  # e.g. conan "requires"
    raw: str  # original declaration text


class PackageManagerAdapter(ABC):
    name: PackageManagerName
    manifest_name: str

    @classmethod
    @abstractmethod
    def detect(cls, repo_root: Path) -> bool:
        """True if this adapter's manifest is present at the repository root."""

    @abstractmethod
    def read_dependencies(self, repo_root: Path) -> list[DeclaredDependency]:
        """All direct declarations. Raises UnsupportedManifest for unsafe formats."""

    def find_dependency(self, repo_root: Path, name: str) -> DeclaredDependency:
        matches = [d for d in self.read_dependencies(repo_root) if d.name == name]
        if not matches:
            raise DependencyNotFound(f"{name} is not a direct {self.name} dependency")
        if len(matches) > 1:
            raws = ", ".join(m.raw for m in matches)
            raise AmbiguousDependency(f"{name} is declared {len(matches)} times: {raws}")
        return matches[0]

    @abstractmethod
    def update_dependency(
        self, repo_root: Path, name: str, target_version: str
    ) -> ManifestChange:
        """Rewrite only this direct dependency's version in place; return the diff."""

    @abstractmethod
    def install_args(self, repo_root: Path, *, build_type: str = "Release") -> list[str]:
        """Command (argv list) that resolves/installs dependencies for the build."""

    @abstractmethod
    def cmake_preset(self, *, build_type: str = "Release") -> str | None:
        """CMake configure/build/test preset produced by install, if any."""

    def command_env(self) -> dict[str, str]:
        """Extra environment variables every install/configure/build/test command
        must receive (e.g. an isolated CONAN_HOME). Merge into ``minimal_env()``.
        Always includes ``CMAKE_EXPORT_COMPILE_COMMANDS=ON`` (CMake >= 3.17 reads it from
        the environment), so every configure, preset or not, writes compile_commands.json."""
        return dict(COMPILE_DB_ENV)

    def cmake_configure_args(self, repo_root: Path, *, build_type: str = "Release") -> list[str]:
        """Extra ``cmake`` configure arguments when ``cmake_preset()`` is None
        (e.g. the vcpkg toolchain file). Empty when the preset carries everything."""
        return []

    def lockfiles(self, repo_root: Path) -> list[str]:
        """Repo-relative lockfiles the adapter itself maintains (part of the dependency change)."""
        return []

    def environment_problems(self) -> list[str]:
        """Tooling problems that make install/build impossible (checked before any edit)."""
        return []


COMPILE_DB_ENV = {"CMAKE_EXPORT_COMPILE_COMMANDS": "ON"}

# Only these variables reach repository builds/tests: never tokens or API keys.
_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CC", "CXX", "CMAKE_GENERATOR")


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Allow-listed environment for subprocesses that run repository code."""
    env = {k: v for k in _PASSTHROUGH_ENV if (v := os.environ.get(k)) is not None}
    env.update(extra or {})
    return env


def unified_diff(rel_path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )
