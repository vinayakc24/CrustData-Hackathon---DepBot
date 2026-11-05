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

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from depbot.models import ReleaseEvent, RepositoryRecord, UpgradePolicy


class RegistryError(ValueError):
    pass


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fleet_id: str
    repositories: list[RepositoryRecord]
    policies: dict[str, UpgradePolicy] = Field(default_factory=dict)
    default_policy_id: str = "default"

    @model_validator(mode="after")
    def _check(self) -> FleetConfig:
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
        """Absolute local path of a registered repository. Remote URLs are P1."""
        ref = repo.repo_url_or_path
        if "://" in ref or ref.startswith("git@"):
            raise RegistryError(f"{repo.repository_id}: remote repositories are not supported yet")
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()


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
