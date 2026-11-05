"""Fleet registry loaded from a small YAML file (plan.md §7).

Example::

    fleet_id: demo-fleet
    policies:
      demo: {allowed_change_levels: [patch, minor, major]}
    repositories:
      - repository_id: repo_a
        repo_url_or_path: ../tests/fixtures/repos/repo_a   # relative to this file
        display_name: Repo A
        policy_id: demo
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from depbot.models import ReleaseEvent, RepositoryRecord, UpgradePolicy
from depbot.repository.remote import RemoteRepositoryError, SyncResult, is_remote, sync_remote


class RegistryError(ValueError):
    pass


class DependencyWatch(BaseModel):
    """An upstream project `depbot monitor` polls for new releases."""

    model_config = ConfigDict(extra="forbid")

    dependency_name: str  # as declared in manifests, e.g. "fmt"
    upstream_repo: str  # GitHub "owner/name"
    tag_prefix: str = ""  # e.g. "fmt-" when release tags look like "fmt-11.0.2"
    enabled: bool = True

    @model_validator(mode="after")
    def _repo_shape(self) -> DependencyWatch:
        parts = self.upstream_repo.strip().strip("/").split("/")
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
            raise ValueError(f"upstream_repo must be 'owner/name', got {self.upstream_repo!r}")
        return self


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fleet_id: str
    repositories: list[RepositoryRecord]
    policies: dict[str, UpgradePolicy] = Field(default_factory=dict)
    default_policy_id: str = "default"
    watches: list[DependencyWatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> FleetConfig:
        watched = [w.dependency_name for w in self.watches]
        if len(watched) != len(set(watched)):
            raise ValueError("each dependency may be watched only once")
        ids = [r.repository_id for r in self.repositories]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate repository_id: {sorted(dupes)}")
        for pid, policy in self.policies.items():
            if policy.policy_id != pid:
                self.policies[pid] = policy.model_copy(update={"policy_id": pid})
        self.policies.setdefault("default", UpgradePolicy(policy_id="default"))
        for repo in self.repositories:
            pid = repo.policy_id or self.default_policy_id
            if pid not in self.policies:
                raise ValueError(f"{repo.repository_id}: unknown policy_id {pid!r}")
        return self


class Fleet:
    """A loaded fleet: config plus the directory relative paths resolve against."""

    def __init__(self, config: FleetConfig, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir.resolve()

    @property
    def fleet_id(self) -> str:
        return self.config.fleet_id

    @property
    def repositories(self) -> list[RepositoryRecord]:
        return list(self.config.repositories)

    def get(self, repository_id: str) -> RepositoryRecord:
        for repo in self.config.repositories:
            if repo.repository_id == repository_id:
                return repo
        raise RegistryError(f"unknown repository {repository_id!r}")

    def policy_for(self, repo: RepositoryRecord) -> UpgradePolicy:
        return self.config.policies[repo.policy_id or self.config.default_policy_id]

    def local_root(self, repo: RepositoryRecord) -> Path:
        """Absolute local path of a locally registered repository (no network access)."""
        ref = repo.repo_url_or_path
        if is_remote(ref):
            raise RegistryError(
                f"{repo.repository_id}: remote repository needs a sync (use resolve_root with a sources dir)"
            )
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()

    def resolve_root(self, repo: RepositoryRecord, sources_dir: Path | None) -> tuple[Path, SyncResult | None]:
        """Local path to read the repository from. A remote repository is cloned or
        fetched into ``sources_dir`` (its DepBot-owned cache) first."""
        if not is_remote(repo.repo_url_or_path):
            return self.local_root(repo), None
        if sources_dir is None:
            raise RegistryError(f"{repo.repository_id}: remote repositories need a state directory to sync into")
        try:
            result = sync_remote(repo, sources_dir)
        except RemoteRepositoryError as exc:
            raise RegistryError(f"{repo.repository_id}: {exc}") from exc
        return Path(result.path), result


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{path} must contain a mapping")
    return data


def load_fleet(path: Path) -> Fleet:
    try:
        config = FleetConfig.model_validate(_load_yaml(path))
    except ValueError as exc:
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError(f"invalid fleet config {path}: {exc}") from exc
    return Fleet(config, path.parent)


def load_event(path: Path) -> ReleaseEvent:
    """Load a normalized ReleaseEvent fixture; changelog_path resolves relative to it."""
    data = _load_yaml(path)
    try:
        event = ReleaseEvent.model_validate(data)
    except ValueError as exc:
        raise RegistryError(f"invalid release event {path}: {exc}") from exc
    if event.changelog_path and not Path(event.changelog_path).is_absolute():
        resolved = (path.parent / event.changelog_path).resolve()
        event = event.model_copy(update={"changelog_path": str(resolved)})
    return event
