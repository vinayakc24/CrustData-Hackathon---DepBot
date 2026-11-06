"""vcpkg adapter: manifest mode (``vcpkg.json``) only (docs/plan.md §3.2, docs/instructions.md §9).

Version model (vcpkg "versioning"):
  * resolved version = max(baseline version, every ``version>=`` constraint),
    unless an ``overrides`` entry pins it exactly;
  * a dependency with neither ``version>=`` nor an override gets whatever the
    ``builtin-baseline`` commit says — DepBot cannot know or change that safely.

Safety rules implemented here:
  * Only the requested direct dependency's ``version>=`` and/or override changes.
  * ``builtin-baseline`` / registry baselines are never edited.
  * Dependencies pinned only by the baseline are refused with an explanation.
  * Edits are byte-minimal (only the version string tokens change) and the
    result is re-parsed and compared with the intended document.
  * Whether the target actually resolves is proven only by ``vcpkg install``
    (the verification install step), never assumed.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from depbot.models import ManifestChange, PackageManagerName
from depbot.package_managers.base import (
    DeclaredDependency,
    PackageManagerAdapter,
    UnsupportedManifest,
    unified_diff,
)

UNPINNED = "unpinned(builtin-baseline)"
CONFIG_NAME = "vcpkg-configuration.json"
INSTALL_DIR = "vcpkg_installed"
_SAFE_VERSION = re.compile(r"[A-Za-z0-9_.+-]+")
_PASS_ENV = ("VCPKG_ROOT", "VCPKG_DEFAULT_TRIPLET", "VCPKG_DEFAULT_HOST_TRIPLET", "VCPKG_DEFAULT_BINARY_CACHE",
             "VCPKG_FORCE_SYSTEM_BINARIES", "VCPKG_DOWNLOADS")  # ARM Linux needs FORCE_SYSTEM_BINARIES

JsonPath = tuple[str | int, ...]


# ----------------------------------------------------------------- JSON spans


class _Scanner:
    """Minimal strict-JSON scanner that records the text span of every scalar value."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.spans: dict[JsonPath, tuple[int, int]] = {}

    def scan(self) -> dict[JsonPath, tuple[int, int]]:
        self._value(())
        self._ws()
        if self.pos != len(self.text):
            raise ValueError(f"trailing data at offset {self.pos}")
        return self.spans

    def _ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def _value(self, path: JsonPath) -> None:
        self._ws()
        ch = self.text[self.pos : self.pos + 1]
        if ch == "{":
            self._object(path)
        elif ch == "[":
            self._array(path)
        else:
            start = self.pos
            if ch == '"':
                self._string()
            else:
                match = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null").match(self.text, self.pos)
                if match is None:
                    raise ValueError(f"unexpected character at offset {self.pos}")
                self.pos = match.end()
            self.spans[path] = (start, self.pos)

    def _string(self) -> str:
        start = self.pos
        self.pos += 1
        while True:
            if self.pos >= len(self.text):
                raise ValueError("unterminated string")
            ch = self.text[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            self.pos += 1
            if ch == '"':
                return json.loads(self.text[start : self.pos])

    def _expect(self, ch: str) -> None:
        self._ws()
        if self.text[self.pos : self.pos + 1] != ch:
            raise ValueError(f"expected {ch!r} at offset {self.pos}")
        self.pos += 1

    def _object(self, path: JsonPath) -> None:
        self._expect("{")
        self._ws()
        if self.text[self.pos : self.pos + 1] == "}":
            self.pos += 1
            return
        while True:
            self._ws()
            key = self._string()
            self._expect(":")
            self._value((*path, key))
            self._ws()
            if self.text[self.pos : self.pos + 1] == ",":
                self.pos += 1
                continue
            self._expect("}")
            return

    def _array(self, path: JsonPath) -> None:
        self._expect("[")
        self._ws()
        if self.text[self.pos : self.pos + 1] == "]":
            self.pos += 1
            return
        index = 0
        while True:
            self._value((*path, index))
            index += 1
            self._ws()
            if self.text[self.pos : self.pos + 1] == ",":
                self.pos += 1
                continue
            self._expect("]")
            return


def replace_values(text: str, edits: dict[JsonPath, Any]) -> str:
    """Replace existing scalar values in ``text`` at ``edits`` paths, touching nothing else."""
    spans = _Scanner(text).scan()
    missing = [p for p in edits if p not in spans]
    if missing:
        raise UnsupportedManifest(f"cannot locate {missing} in vcpkg.json")
    out = text
    for path in sorted(edits, key=lambda p: spans[p][0], reverse=True):
        start, end = spans[path]
        out = out[:start] + json.dumps(edits[path], ensure_ascii=False) + out[end:]
    return out


def _set(doc: Any, path: JsonPath, value: Any) -> None:
    for key in path[:-1]:
        doc = doc[key]
    doc[path[-1]] = value


def _base_version(version: str) -> str:
    """vcpkg 'version#port-version' → 'version'."""
    return version.split("#", 1)[0]


# --------------------------------------------------------------------- adapter


class VcpkgAdapter(PackageManagerAdapter):
    name = PackageManagerName.VCPKG
    manifest_name = "vcpkg.json"

    @classmethod
    def detect(cls, repo_root: Path) -> bool:
        return (repo_root / cls.manifest_name).is_file()

    # -- reading -----------------------------------------------------------------

    def _load(self, repo_root: Path) -> tuple[str, dict]:
        path = repo_root / self.manifest_name
        if not path.is_file():
            raise UnsupportedManifest(f"{self.manifest_name} not found in {repo_root}")
        text = path.read_bytes().decode("utf-8")  # bytes: keep CRLF exactly
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UnsupportedManifest(f"vcpkg.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise UnsupportedManifest("vcpkg.json must be a JSON object")
        if not isinstance(data.get("dependencies", []), list):
            raise UnsupportedManifest("vcpkg.json 'dependencies' must be a list")
        if not isinstance(data.get("overrides", []), list):
            raise UnsupportedManifest("vcpkg.json 'overrides' must be a list")
        return text, data

    def baseline(self, repo_root: Path) -> str | None:
        """Baseline that anchors versioning: builtin-baseline or the default registry's."""
        _, data = self._load(repo_root)
        if isinstance(data.get("builtin-baseline"), str):
            return data["builtin-baseline"]
        configs = [data.get("vcpkg-configuration")]
        config_path = repo_root / CONFIG_NAME
        if config_path.is_file():
            try:
                configs.append(json.loads(config_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                raise UnsupportedManifest(f"{CONFIG_NAME} is not valid JSON: {exc}") from exc
        for config in configs:
            if isinstance(config, dict):
                registry = config.get("default-registry")
                if isinstance(registry, dict) and isinstance(registry.get("baseline"), str):
                    return registry["baseline"]
        return None

    def _overrides(self, data: dict) -> dict[str, tuple[int, dict]]:
        result: dict[str, tuple[int, dict]] = {}
        for index, entry in enumerate(data.get("overrides", [])):
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise UnsupportedManifest(f"unrecognised vcpkg override: {entry!r}")
            if entry["name"] in result:
                raise UnsupportedManifest(f"{entry['name']} is overridden more than once")
            result[entry["name"]] = (index, entry)
        return result

    def read_dependencies(self, repo_root: Path) -> list[DeclaredDependency]:
        _, data = self._load(repo_root)
        overrides = self._overrides(data)
        deps: list[DeclaredDependency] = []
        for entry in data.get("dependencies", []):
            if isinstance(entry, str):
                name, minimum, host = entry, None, False
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                name, minimum, host = entry["name"], entry.get("version>="), bool(entry.get("host"))
            else:
                raise UnsupportedManifest(f"unrecognised vcpkg dependency: {entry!r}")
            override = overrides.get(name, (None, {}))[1]
            pinned = override.get("version")
            if pinned is not None and not isinstance(pinned, str):
                raise UnsupportedManifest(f"override for {name} must use a string 'version'")
            if pinned is None and any(k in override for k in ("version-semver", "version-date", "version-string")):
                raise UnsupportedManifest(f"override for {name} uses a legacy version key; update manually")
            version = pinned or minimum
            deps.append(
                DeclaredDependency(
                    name=name,
                    version=_base_version(version) if version else UNPINNED,
                    manifest_path=self.manifest_name,
                    package_manager=self.name,
                    is_direct=True,
                    section="host-dependencies" if host else "dependencies",
                    raw=json.dumps(entry, ensure_ascii=False)
                    + (f" + override {json.dumps(override, ensure_ascii=False)}" if override else ""),
                )
            )
        return deps

    # -- updating ----------------------------------------------------------------

    def update_dependency(self, repo_root: Path, name: str, target_version: str) -> ManifestChange:
        if not _SAFE_VERSION.fullmatch(target_version):
            raise UnsupportedManifest(f"refusing suspicious target version {target_version!r}")
        current = self.find_dependency(repo_root, name)  # raises if missing/ambiguous
        if current.version == UNPINNED:
            raise UnsupportedManifest(
                f"{name} has no 'version>=' constraint or override; its version comes only from "
                "the baseline. DepBot never moves builtin-baseline (it would upgrade every "
                "dependency). Add a 'version>=' constraint for this dependency or update the "
                "baseline manually."
            )
        if self.baseline(repo_root) is None:
            raise UnsupportedManifest(
                "vcpkg versioning ('version>=' / overrides) requires a builtin-baseline or a "
                "default-registry baseline; none is configured"
            )
        path = repo_root / self.manifest_name
        before, data = self._load(repo_root)
        expected = copy.deepcopy(data)
        edits: dict[JsonPath, Any] = {}
        for index, entry in enumerate(data.get("dependencies", [])):
            if isinstance(entry, dict) and entry.get("name") == name and "version>=" in entry:
                edits[("dependencies", index, "version>=")] = target_version
        override = self._overrides(data).get(name)
        if override is not None:
            index, entry = override
            edits[("overrides", index, "version")] = target_version
            if "port-version" in entry:
                edits[("overrides", index, "port-version")] = 0  # new upstream version resets it
        for json_path, value in edits.items():
            _set(expected, json_path, value)
        after = replace_values(before, edits)
        if json.loads(after) != expected:  # defensive: the text edit must mean exactly this
            raise UnsupportedManifest("internal check failed: edited vcpkg.json differs from intent")
        path.write_bytes(after.encode("utf-8"))
        return ManifestChange(
            manifest_path=self.manifest_name,
            dependency_name=name,
            from_version=current.version,
            to_version=target_version,
            unified_diff=unified_diff(self.manifest_name, before, after),
        )

    # -- commands ----------------------------------------------------------------

    def _root(self) -> Path | None:
        root = os.environ.get("VCPKG_ROOT")
        return Path(root).resolve() if root else None

    def _executable(self) -> str:
        root = self._root()
        if root is not None and (root / "vcpkg").is_file():
            return str(root / "vcpkg")
        return "vcpkg"

    def install_args(self, repo_root: Path, *, build_type: str = "Release") -> list[str]:
        self._load(repo_root)
        root = repo_root.resolve()
        return [
            self._executable(),
            "install",
            f"--x-manifest-root={root}",
            f"--x-install-root={root / INSTALL_DIR}",
        ]

    def cmake_preset(self, *, build_type: str = "Release") -> str | None:
        return None  # vcpkg does not generate CMake presets; see cmake_configure_args

    def cmake_configure_args(self, repo_root: Path, *, build_type: str = "Release") -> list[str]:
        root = self._root()
        if root is None:
            raise UnsupportedManifest("VCPKG_ROOT is not set; cannot locate vcpkg.cmake")
        return [
            f"-DCMAKE_TOOLCHAIN_FILE={root / 'scripts' / 'buildsystems' / 'vcpkg.cmake'}",
            "-DVCPKG_MANIFEST_INSTALL=OFF",  # dependencies were installed by install_args
            f"-DVCPKG_INSTALLED_DIR={repo_root.resolve() / INSTALL_DIR}",
            f"-DCMAKE_BUILD_TYPE={build_type}",
        ]

    def command_env(self) -> dict[str, str]:
        env = super().command_env()
        env.update({k: v for k in _PASS_ENV if (v := os.environ.get(k))})
        env["VCPKG_DISABLE_METRICS"] = "1"
        return env

    def environment_problems(self) -> list[str]:
        root = self._root()
        problems: list[str] = []
        if root is None:
            problems.append("VCPKG_ROOT is not set")
        elif not (root / "scripts" / "buildsystems" / "vcpkg.cmake").is_file():
            problems.append(f"VCPKG_ROOT={root} has no scripts/buildsystems/vcpkg.cmake")
        if shutil.which(self._executable()) is None and not Path(self._executable()).is_file():
            problems.append("vcpkg executable not found (set VCPKG_ROOT or add vcpkg to PATH)")
        return problems
