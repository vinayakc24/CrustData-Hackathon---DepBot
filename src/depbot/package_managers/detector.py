"""Choose the package-manager adapter for a repository, or refuse explicitly."""

from __future__ import annotations

from pathlib import Path

from depbot.package_managers.base import PackageManagerAdapter, UnsupportedManifest
from depbot.package_managers.conan import ConanAdapter

ADAPTERS: tuple[type[PackageManagerAdapter], ...] = (ConanAdapter,)


def detect_adapter(repo_root: Path) -> PackageManagerAdapter:
    found = [cls for cls in ADAPTERS if cls.detect(repo_root)]
    if len(found) > 1:
        raise UnsupportedManifest(
            "multiple package managers detected: " + ", ".join(c.name for c in found)
        )
    if found:
        return found[0]()
    if (repo_root / "vcpkg.json").is_file():
        # Not faked: vcpkg support is not implemented yet (plan.md §16 item 3).
        raise UnsupportedManifest("vcpkg.json detected; vcpkg adapter is not implemented yet")
    if (repo_root / "conanfile.py").is_file():
        raise UnsupportedManifest("conanfile.py is executable; only conanfile.txt is supported")
    raise UnsupportedManifest(f"no supported package manifest in {repo_root}")
