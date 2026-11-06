"""Dependency inventory: which repository declares which dependency at which version.

Backed by one JSON file. The index is a candidate-selection accelerator, never
proof of current contents: entries are keyed by the indexed commit and rescanned
when the repository's HEAD (or manifest content) changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from depbot.models import RepositoryDependencyRecord, RepositoryRecord
from depbot.package_managers.base import PackageManagerError
from depbot.package_managers.detector import detect_adapter
from depbot.repository.git_info import head_commit


class RepositoryScan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: str
    indexed_commit: str | None
    manifest_digest: str | None
    dependencies: list[RepositoryDependencyRecord] = Field(default_factory=list)
    error: str | None = None  # explained refusal from the package-manager adapter


# Bump when manifest parsing changes, so cached scans (and refusals) are redone.
SCANNER_VERSION = "2"


def _manifest_digest(root: Path) -> str | None:
    digest = hashlib.sha256(f"scanner-{SCANNER_VERSION}\0".encode())
    found = False
    for name in ("conanfile.txt", "conanfile.py", "conan.lock", "vcpkg.json", "vcpkg-configuration.json"):
        path = root / name
        if path.is_file():
            found = True
            digest.update(name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16] if found else None


def scan_repository(repo: RepositoryRecord, root: Path) -> RepositoryScan:
    commit = head_commit(root)
    digest = _manifest_digest(root)
    try:
        adapter = detect_adapter(root)
        declared = adapter.read_dependencies(root)
    except PackageManagerError as exc:
        return RepositoryScan(
            repository_id=repo.repository_id,
            indexed_commit=commit,
            manifest_digest=digest,
            error=str(exc),
        )
    records = [
        RepositoryDependencyRecord(
            repository_id=repo.repository_id,
            dependency_name=d.name,
            current_version=d.version,
            package_manager=d.package_manager,
            manifest_path=d.manifest_path,
            is_direct=d.is_direct,
            indexed_commit=commit,
        )
        for d in declared
    ]
    return RepositoryScan(
        repository_id=repo.repository_id,
        indexed_commit=commit,
        manifest_digest=digest,
        dependencies=records,
    )


class DependencyIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._scans: dict[str, RepositoryScan] = {}
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._scans = {k: RepositoryScan.model_validate(v) for k, v in raw.items()}

    def get(self, repository_id: str) -> RepositoryScan | None:
        return self._scans.get(repository_id)

    def is_stale(self, repo: RepositoryRecord, root: Path) -> bool:
        scan = self._scans.get(repo.repository_id)
        if scan is None:
            return True
        return scan.indexed_commit != head_commit(root) or scan.manifest_digest != _manifest_digest(
            root
        )

    def refresh(self, repo: RepositoryRecord, root: Path, *, force: bool = False) -> tuple[RepositoryScan, bool]:
        """Return (scan, rescanned). Rescans only when stale unless forced."""
        if not force and not self.is_stale(repo, root):
            return self._scans[repo.repository_id], False
        scan = scan_repository(repo, root)
        self._scans[repo.repository_id] = scan
        return scan, True

    def users_of(self, dependency_name: str) -> list[RepositoryDependencyRecord]:
        return [
            d
            for scan in self._scans.values()
            for d in scan.dependencies
            if d.dependency_name == dependency_name
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in sorted(self._scans.items())}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
