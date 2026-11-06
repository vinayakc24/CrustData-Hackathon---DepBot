"""Conan 2 adapter: ``conanfile.txt`` only (P0). ``conanfile.py`` is refused because
it is executable Python whose requirements cannot be rewritten safely."""

from __future__ import annotations

import json
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
LOCKFILE = "conan.lock"
_LOCK_SECTIONS = ("requires", "build_requires", "python_requires")


def _read(path: Path) -> str:
    # Bytes in, bytes out: keeps CRLF line endings exactly (read_text would normalise them).
    return path.read_bytes().decode("utf-8")


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
        text = _read(self._manifest(repo_root))
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
            if "/[" in body:  # version ranges may contain spaces: `name/[>=1.0 <2.0]`
                raise UnsupportedManifest(
                    f"version range in {body!r} cannot be updated safely; pin an exact version first"
                )
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
        before = _read(path)
        lines = before.splitlines(keepends=True)
        for index, dep in self._iter_refs(before):
            if dep.name == name:
                old = f"{name}/{dep.version}"
                if old not in lines[index]:
                    raise UnsupportedManifest(f"cannot locate {old!r} on its line")
                lines[index] = lines[index].replace(old, f"{name}/{target_version}", 1)
        after = "".join(lines)
        path.write_bytes(after.encode("utf-8"))
        self._unpin_locked(repo_root, name)
        return ManifestChange(
            manifest_path=self.manifest_name,
            dependency_name=name,
            from_version=current.version,
            to_version=target_version,
            unified_diff=unified_diff(self.manifest_name, before, after),
        )

    def _unpin_locked(self, repo_root: Path, name: str) -> None:
        """Drop only ``name``'s pins from ``conan.lock``; ``install_args`` then resolves it
        partially and rewrites the lock, so every other locked version stays exactly as is."""
        lock = repo_root / LOCKFILE
        if not lock.is_file():
            return
        try:
            data = json.loads(_read(lock))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UnsupportedManifest(f"{LOCKFILE} is not valid JSON: {exc}") from exc
        for section in _LOCK_SECTIONS:
            refs = data.get(section)
            if isinstance(refs, list):
                data[section] = [r for r in refs if not (isinstance(r, str) and r.split("/", 1)[0] == name)]
        lock.write_bytes((json.dumps(data, indent=4) + "\n").encode("utf-8"))

    def lockfiles(self, repo_root: Path) -> list[str]:
        return [LOCKFILE] if (repo_root / LOCKFILE).is_file() else []

    def install_args(self, repo_root: Path, *, build_type: str = "Release") -> list[str]:
        self._manifest(repo_root)
        args = ["conan", "install", ".", "--build=missing", "-s", f"build_type={build_type}"]
        if (repo_root / LOCKFILE).is_file():
            # Locked versions are honoured; the unpinned upgraded dependency is resolved and
            # the lock rewritten in place (it is part of the PR).
            args += ["--lockfile", LOCKFILE, "--lockfile-partial", "--lockfile-out", LOCKFILE]
        return args

    def cmake_preset(self, *, build_type: str = "Release") -> str | None:
        # Requires `[layout] cmake_layout` + CMakeToolchain in conanfile.txt.
        return f"conan-{build_type.lower()}"

    def command_env(self) -> dict[str, str]:
        env = super().command_env()
        home = os.environ.get(CONAN_HOME_ENV)
        if home:
            env["CONAN_HOME"] = str(Path(home).resolve())
        return env

    def environment_problems(self) -> list[str]:
        return [] if shutil.which("conan") else ["conan executable not found on PATH"]
