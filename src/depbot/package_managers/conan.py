"""Conan 2 adapter: ``conanfile.txt`` only (P0). ``conanfile.py`` is refused because
it is executable Python whose requirements cannot be rewritten safely."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from depbot.models import ManifestChange, PackageManagerName
from depbot.package_managers.base import (
    DeclaredDependency,
    PackageManagerAdapter,
    UnsupportedManifest,
    unified_diff,
)

_SECTION = re.compile(r"^\s*\[(?P<name>[A-Za-z_]+)\]\s*$")
# name/version[@user/channel][#revision]
_REF = re.compile(
    r"^(?P<name>[a-z0-9_][a-z0-9_+.-]*)/(?P<version>[^@#\s]+)(?P<rest>(@[^#\s]+)?(#\S+)?)$",
    re.IGNORECASE,
)
_DIRECT_SECTIONS = {"requires", "tool_requires", "test_requires"}
# When set, DepBot uses this isolated Conan home instead of the user's ~/.conan2.
CONAN_HOME_ENV = "DEPBOT_CONAN_HOME"


class ConanAdapter(PackageManagerAdapter):
    name = PackageManagerName.CONAN
    manifest_name = "conanfile.txt"

    @classmethod
    def detect(cls, repo_root: Path) -> bool:
        return (repo_root / cls.manifest_name).is_file()

    def _manifest(self, repo_root: Path) -> Path:
        path = repo_root / self.manifest_name
        if not path.is_file():
            if (repo_root / "conanfile.py").is_file():
                raise UnsupportedManifest(
                    "conanfile.py is executable; only conanfile.txt is supported (P0)"
                )
            raise UnsupportedManifest(f"{self.manifest_name} not found in {repo_root}")
        return path

    def read_dependencies(self, repo_root: Path) -> list[DeclaredDependency]:
        text = self._manifest(repo_root).read_text(encoding="utf-8")
        return [dep for _, dep in self._iter_refs(text)]

    def _iter_refs(self, text: str):
        section: str | None = None
        for index, line in enumerate(text.splitlines()):
            match = _SECTION.match(line)
            if match:
                section = match["name"].lower()
                continue
            if section not in _DIRECT_SECTIONS:
                continue
            # Whole-line comments only; '#' after a reference is a recipe revision.
            body = line.strip()
            if not body or body.startswith("#"):
                continue
            ref = _REF.match(body)
            if ref is None:
                raise UnsupportedManifest(f"unrecognised conan requirement: {line.strip()!r}")
            version = ref["version"]
            if version.startswith("["):
                raise UnsupportedManifest(
                    f"version range {version!r} for {ref['name']} cannot be updated safely"
                )
            yield index, DeclaredDependency(
                name=ref["name"],
                version=version,
                manifest_path=self.manifest_name,
                package_manager=self.name,
                is_direct=True,
                section=section,
                raw=body,
            )

    def update_dependency(
        self, repo_root: Path, name: str, target_version: str
    ) -> ManifestChange:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", target_version):
            raise UnsupportedManifest(f"refusing suspicious target version {target_version!r}")
        current = self.find_dependency(repo_root, name)  # raises if missing/ambiguous
        path = self._manifest(repo_root)
        before = path.read_text(encoding="utf-8")
        lines = before.splitlines(keepends=True)
        for index, dep in self._iter_refs(before):
            if dep.name == name:
                old = f"{name}/{dep.version}"
                if old not in lines[index]:
                    raise UnsupportedManifest(f"cannot locate {old!r} on its line")
                lines[index] = lines[index].replace(old, f"{name}/{target_version}", 1)
        after = "".join(lines)
        path.write_text(after, encoding="utf-8")
        return ManifestChange(
            manifest_path=self.manifest_name,
            dependency_name=name,
            from_version=current.version,
            to_version=target_version,
            unified_diff=unified_diff(self.manifest_name, before, after),
        )

    def install_args(self, repo_root: Path, *, build_type: str = "Release") -> list[str]:
        self._manifest(repo_root)
        return [
            "conan",
            "install",
            ".",
            "--build=missing",
            "-s",
            f"build_type={build_type}",
        ]

    def cmake_preset(self, *, build_type: str = "Release") -> str | None:
        # Requires `[layout] cmake_layout` + CMakeToolchain in conanfile.txt.
        return f"conan-{build_type.lower()}"

    def command_env(self) -> dict[str, str]:
        home = os.environ.get(CONAN_HOME_ENV)
        return {"CONAN_HOME": str(Path(home).resolve())} if home else {}

    def environment_problems(self) -> list[str]:
        return [] if shutil.which("conan") else ["conan executable not found on PATH"]
