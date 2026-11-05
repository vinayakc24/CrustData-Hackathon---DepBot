"""VcpkgAdapter: manifest parsing, byte-minimal safe updates, refusals, commands.

These tests do not run vcpkg itself (not available in CI/sandbox); install and
toolchain arguments are asserted as argv, and real resolution is the
verification stage's job.
"""

import json
import textwrap

import pytest

from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.registry import load_event, load_fleet
from depbot.fleet.selection import select_repositories
from depbot.models import FleetClassification, OutcomeStatus
from depbot.package_managers.base import AmbiguousDependency, DependencyNotFound, UnsupportedManifest
from depbot.package_managers.detector import detect_adapter
from depbot.package_managers.vcpkg import UNPINNED, VcpkgAdapter, replace_values

BASELINE = "3426db05b996481ca31e95fff3734cf23e0f51bc"

# Deliberately non-canonical formatting: edits must leave every other byte alone.
MANIFEST = """{
  "name": "repo-a",
  "version-string": "0.1.0",
  "builtin-baseline": "%s",
  "dependencies": [
    "zlib",
    { "name": "toydep", "version>=": "1.0.0" },
    {
      "name": "fmt",
      "version>=": "10.2.1#2",
      "features": [ "std-format" ]
    },
    { "name": "gtest", "host": true, "version>=": "1.0.0" }
  ],
  "overrides": [
    { "name": "spdlog", "version": "1.12.0", "port-version": 3 }
  ]
}
""" % BASELINE


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "vcpkg.json").write_text(MANIFEST)
    return tmp_path


def _read(repo):
    return {d.name: d for d in VcpkgAdapter().read_dependencies(repo)}


def test_reads_dependencies_constraints_and_host(repo):
    deps = _read(repo)
    assert deps["toydep"].version == "1.0.0"
    assert deps["fmt"].version == "10.2.1"  # '#2' port-version stripped for comparison
    assert deps["zlib"].version == UNPINNED  # baseline-only
    assert deps["gtest"].section == "host-dependencies"
    assert "spdlog" not in deps  # override without a direct dependency is not direct
    assert all(d.package_manager.value == "vcpkg" and d.manifest_path == "vcpkg.json" for d in deps.values())


def test_update_changes_only_the_target_constraint_bytes(repo):
    change = VcpkgAdapter().update_dependency(repo, "toydep", "2.0.0")
    after = (repo / "vcpkg.json").read_text()
    assert after == MANIFEST.replace('"version>=": "1.0.0" }', '"version>=": "2.0.0" }', 1)
    assert BASELINE in after  # baseline untouched
    assert change.from_version == "1.0.0" and change.to_version == "2.0.0"
    body = change.unified_diff.splitlines()[2:]  # skip ---/+++ headers
    assert [l for l in body if l.startswith(("-", "+"))] == [
        '-    { "name": "toydep", "version>=": "1.0.0" },',
        '+    { "name": "toydep", "version>=": "2.0.0" },',
    ]


def test_update_drops_port_version_suffix(repo):
    VcpkgAdapter().update_dependency(repo, "fmt", "11.0.2")
    data = json.loads((repo / "vcpkg.json").read_text())
    assert data["dependencies"][2]["version>="] == "11.0.2"
    assert data["dependencies"][2]["features"] == ["std-format"]


def test_override_pin_is_updated_and_port_version_reset(tmp_path):
    (tmp_path / "vcpkg.json").write_text(json.dumps({
        "builtin-baseline": BASELINE,
        "dependencies": ["toydep"],
        "overrides": [{"name": "toydep", "version": "1.0.0", "port-version": 2}],
    }, indent=4) + "\n")
    adapter = VcpkgAdapter()
    assert adapter.find_dependency(tmp_path, "toydep").version == "1.0.0"
    adapter.update_dependency(tmp_path, "toydep", "2.0.0")
    data = json.loads((tmp_path / "vcpkg.json").read_text())
    assert data["overrides"] == [{"name": "toydep", "version": "2.0.0", "port-version": 0}]
    assert data["dependencies"] == ["toydep"]


def test_baseline_only_dependency_is_refused_without_editing(repo):
    with pytest.raises(UnsupportedManifest, match="never moves builtin-baseline"):
        VcpkgAdapter().update_dependency(repo, "zlib", "1.3.1")
    assert (repo / "vcpkg.json").read_text() == MANIFEST


def test_versioning_without_any_baseline_is_refused(tmp_path):
    (tmp_path / "vcpkg.json").write_text('{"dependencies": [{"name": "toydep", "version>=": "1.0.0"}]}')
    with pytest.raises(UnsupportedManifest, match="requires a builtin-baseline"):
        VcpkgAdapter().update_dependency(tmp_path, "toydep", "2.0.0")


def test_registry_baseline_from_configuration_file_is_accepted(tmp_path):
    (tmp_path / "vcpkg.json").write_text('{"dependencies": [{"name": "toydep", "version>=": "1.0.0"}]}')
    (tmp_path / "vcpkg-configuration.json").write_text(json.dumps({
        "default-registry": {"kind": "git", "repository": "https://github.com/microsoft/vcpkg",
                             "baseline": BASELINE}}))
    adapter = VcpkgAdapter()
    assert adapter.baseline(tmp_path) == BASELINE
    adapter.update_dependency(tmp_path, "toydep", "2.0.0")
    assert '"version>=": "2.0.0"' in (tmp_path / "vcpkg.json").read_text()


def test_missing_ambiguous_and_malformed_inputs(tmp_path, repo):
    with pytest.raises(DependencyNotFound):
        VcpkgAdapter().update_dependency(repo, "boost", "1.0.0")
    with pytest.raises(UnsupportedManifest):
        VcpkgAdapter().update_dependency(repo, "toydep", "2.0.0;rm -rf /")
    (tmp_path / "vcpkg.json").write_text(json.dumps({"builtin-baseline": BASELINE, "dependencies": [
        {"name": "toydep", "version>=": "1.0.0", "platform": "windows"},
        {"name": "toydep", "version>=": "1.1.0", "platform": "!windows"}]}))
    with pytest.raises(AmbiguousDependency):
        VcpkgAdapter().update_dependency(tmp_path, "toydep", "2.0.0")
    (tmp_path / "vcpkg.json").write_text("{ not json")
    with pytest.raises(UnsupportedManifest, match="not valid JSON"):
        VcpkgAdapter().read_dependencies(tmp_path)
    (tmp_path / "vcpkg.json").write_text('{"dependencies": [42]}')
    with pytest.raises(UnsupportedManifest, match="unrecognised"):
        VcpkgAdapter().read_dependencies(tmp_path)
    (tmp_path / "vcpkg.json").write_text('{"dependencies": ["a"], "overrides": [{"name": "a", "version-semver": "1.0.0"}]}')
    with pytest.raises(UnsupportedManifest, match="legacy version key"):
        VcpkgAdapter().read_dependencies(tmp_path)


def test_replace_values_handles_escapes_and_nesting():
    text = '{"a": "x\\"y", "b": [1, {"c": "old"}], "d": true}'
    assert replace_values(text, {("b", 1, "c"): "new"}) == '{"a": "x\\"y", "b": [1, {"c": "new"}], "d": true}'
    with pytest.raises(UnsupportedManifest):
        replace_values(text, {("nope",): 1})


def test_commands_env_and_preflight(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("VCPKG_ROOT", raising=False)
    adapter = detect_adapter(repo)
    assert isinstance(adapter, VcpkgAdapter) and adapter.cmake_preset() is None
    assert "VCPKG_ROOT is not set" in adapter.environment_problems()
    with pytest.raises(UnsupportedManifest, match="VCPKG_ROOT"):
        adapter.cmake_configure_args(repo)

    root = tmp_path / "vcpkg-root"
    (root / "scripts" / "buildsystems").mkdir(parents=True)
    (root / "scripts" / "buildsystems" / "vcpkg.cmake").write_text("")
    (root / "vcpkg").write_text("#!/bin/sh\n")
    monkeypatch.setenv("VCPKG_ROOT", str(root))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    assert adapter.environment_problems() == []
    assert adapter.install_args(repo) == [
        str(root / "vcpkg"), "install",
        f"--x-manifest-root={repo.resolve()}", f"--x-install-root={repo.resolve() / 'vcpkg_installed'}"]
    args = adapter.cmake_configure_args(repo, build_type="Debug")
    assert f"-DCMAKE_TOOLCHAIN_FILE={root / 'scripts' / 'buildsystems' / 'vcpkg.cmake'}" in args
    assert "-DVCPKG_MANIFEST_INSTALL=OFF" in args and "-DCMAKE_BUILD_TYPE=Debug" in args
    env = adapter.command_env()
    assert env["VCPKG_ROOT"] == str(root) and env["VCPKG_DISABLE_METRICS"] == "1"
    assert "GITHUB_TOKEN" not in env


def test_fleet_selection_works_for_vcpkg_repositories(tmp_path):
    for name, version in (("repo_v1", "1.0.0"), ("repo_v2", "2.0.0")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "vcpkg.json").write_text(json.dumps({
            "builtin-baseline": BASELINE, "dependencies": [{"name": "toydep", "version>=": version}]}))
    (tmp_path / "repo_unpinned").mkdir()
    (tmp_path / "repo_unpinned" / "vcpkg.json").write_text(json.dumps({"dependencies": ["toydep"]}))
    (tmp_path / "fleet.yaml").write_text(textwrap.dedent("""\
        fleet_id: v
        default_policy_id: p
        policies: {p: {allowed_change_levels: [major], major_approved: true}}
        repositories:
          - {repository_id: repo_v1, repo_url_or_path: repo_v1, display_name: v1}
          - {repository_id: repo_v2, repo_url_or_path: repo_v2, display_name: v2}
          - {repository_id: repo_unpinned, repo_url_or_path: repo_unpinned, display_name: u}
        """))
    (tmp_path / "event.yaml").write_text(
        "event_id: e\ndependency_name: toydep\nupstream_repo: x\nprevious_version: 1.0.0\ntarget_version: 2.0.0\n")
    event = load_event(tmp_path / "event.yaml")
    sel = {s.repository.repository_id: s for s in select_repositories(
        event, load_fleet(tmp_path / "fleet.yaml"), DependencyIndex(tmp_path / "inv.json"))}
    assert sel["repo_v1"].eligible
    assert sel["repo_v2"].classification == FleetClassification.ALREADY_AT_TARGET_OR_NEWER
    assert sel["repo_unpinned"].classification == FleetClassification.NEEDS_RESCAN
    assert sel["repo_unpinned"].to_outcome(event).status == OutcomeStatus.BLOCKED
    assert UNPINNED in sel["repo_unpinned"].reason


def test_engine_blocks_vcpkg_job_when_tooling_missing(tmp_path, monkeypatch):
    from depbot.models import (ReleaseEvent, RepositoryDependencyRecord, RepositoryRecord,
                               RepositoryUpgradeJob, UpgradePolicy)
    from depbot.orchestrator import run_repository_job
    from tests.unit.test_orchestrator import _stages

    monkeypatch.delenv("VCPKG_ROOT", raising=False)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\nproject(x CXX)\n")
    (repo / "vcpkg.json").write_text(json.dumps(
        {"builtin-baseline": BASELINE, "dependencies": [{"name": "toydep", "version>=": "1.0.0"}]}))
    job = RepositoryUpgradeJob(
        job_id="j", repo_root=str(repo),
        event=ReleaseEvent(event_id="e", dependency_name="toydep", upstream_repo="x",
                           previous_version="1.0.0", target_version="2.0.0"),
        repository=RepositoryRecord(repository_id="repo", repo_url_or_path=str(repo), display_name="r"),
        dependency_record=RepositoryDependencyRecord(repository_id="repo", dependency_name="toydep",
                                                     current_version="1.0.0", package_manager="vcpkg",
                                                     manifest_path="vcpkg.json"),
        policy=UpgradePolicy(allowed_change_levels=["major"], major_approved=True),
    )
    state, outcome = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    assert outcome.status == OutcomeStatus.BLOCKED and "vcpkg tooling" in state.status_reason
    assert state.manifest_change is None
    assert '"1.0.0"' in (repo / "vcpkg.json").read_text()
