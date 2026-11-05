"""Remote repositories: URL safety, clone/fetch cache, fleet selection and jobs.

Remotes are local bare repositories reached over file:// (enabled for tests
only; the product allows https/ssh/scp-style URLs). No network is used.
"""

import subprocess
import textwrap

import pytest

import depbot.repository.remote as remote
from depbot.cli import main
from depbot.fleet.dependency_index import DependencyIndex
from depbot.fleet.registry import load_event, load_fleet
from depbot.fleet.runner import run_fleet
from depbot.fleet.selection import select_repositories
from depbot.models import FleetClassification, OutcomeStatus, RepositoryRecord
from depbot.repository.remote import RemoteRepositoryError, cache_dir_for, sync_remote, validate_url
from tests.unit.test_orchestrator import MAIN, _stages

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def allow_file_urls(monkeypatch):
    monkeypatch.setattr(remote, "ALLOWED_SCHEMES", frozenset({"https", "ssh", "file"}))


def make_remote(root, name, version, source=MAIN):
    """Create a bare 'remote' repo containing a CMake + Conan project; return (url, work clone)."""
    bare = root / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "--bare", str(bare))
    work = root / "authors" / name
    git(root, "clone", "-q", str(bare), str(work))
    (work / "src").mkdir()
    (work / "src" / "main.cpp").write_text(source)
    (work / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\nproject(x CXX)\n")
    (work / "conanfile.txt").write_text(f"[requires]\ntoydep/{version}\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return f"file://{bare}", work


# ------------------------------------------------------------------ URL safety


@pytest.mark.parametrize("url", [
    "https://github.com/acme/service", "https://github.com/acme/service.git",
    "git@github.com:acme/service.git", "ssh://git@github.com/acme/service.git",
])
def test_accepted_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url,match", [
    ("http://github.com/a/b", "scheme 'http'"),
    ("file:///etc", "scheme 'file'"),
    ("ext::sh -c touch% /tmp/pwned", "malformed|scheme"),
    ("--upload-pack=touch /tmp/x", "malformed"),
    ("https://user:ghp_secret@github.com/a/b", "must not embed credentials"),
    ("https://x-access-token@github.com/a/b", "must not embed credentials"),
    ("https://github.com/a/b\nhttps://evil", "malformed"),
    ("https:///nohost", "no host"),
])
def test_rejected_urls(url, match):
    with pytest.raises(RemoteRepositoryError, match=match):
        validate_url(url)


def test_cache_dir_is_sanitized_and_url_specific(tmp_path):
    a = RepositoryRecord(repository_id="../../etc", repo_url_or_path="https://github.com/a/b", display_name="x")
    b = a.model_copy(update={"repo_url_or_path": "https://github.com/a/c"})
    da, db = cache_dir_for(a, tmp_path), cache_dir_for(b, tmp_path)
    assert da.parent == tmp_path and ".." not in da.name and da != db


# ------------------------------------------------------------------ sync


def test_clone_then_fetch_tracks_remote(tmp_path, allow_file_urls):
    url, author = make_remote(tmp_path, "svc", "1.0.0")
    repo = RepositoryRecord(repository_id="svc", repo_url_or_path=url, display_name="svc")
    first = sync_remote(repo, tmp_path / "state" / "sources")
    assert first.cloned and first.branch == "main"
    assert git(first.path, "remote", "get-url", "origin") == url  # origin kept for PR delivery

    (author / "conanfile.txt").write_text("[requires]\ntoydep/2.0.0\n")
    git(author, "commit", "-qam", "bump")
    git(author, "push", "-q", "origin", "HEAD:main")
    second = sync_remote(repo, tmp_path / "state" / "sources")
    assert not second.cloned and second.path == first.path and second.commit != first.commit
    assert "toydep/2.0.0" in (tmp_path / "state" / "sources").joinpath(
        first.path.rsplit("/", 1)[-1], "conanfile.txt").read_text()


def test_explicit_branch_and_missing_branch(tmp_path, allow_file_urls):
    url, author = make_remote(tmp_path, "svc", "1.0.0")
    git(author, "checkout", "-q", "-b", "release")
    git(author, "push", "-q", "origin", "release")
    repo = RepositoryRecord(repository_id="svc", repo_url_or_path=url, display_name="s", default_branch="release")
    assert sync_remote(repo, tmp_path / "s").branch == "release"
    missing = repo.model_copy(update={"default_branch": "nope"})
    with pytest.raises(RemoteRepositoryError, match="checkout failed"):
        sync_remote(missing, tmp_path / "s")
    with pytest.raises(RemoteRepositoryError, match="suspicious branch"):
        sync_remote(repo.model_copy(update={"default_branch": "--orphan"}), tmp_path / "s")


def test_edited_cache_is_never_discarded(tmp_path, allow_file_urls):
    url, _ = make_remote(tmp_path, "svc", "1.0.0")
    repo = RepositoryRecord(repository_id="svc", repo_url_or_path=url, display_name="s")
    cache = sync_remote(repo, tmp_path / "s").path
    (tmp_path / "s").joinpath(cache.rsplit("/", 1)[-1], "src", "main.cpp").write_text("// hand edit\n")
    with pytest.raises(RemoteRepositoryError, match="has local changes"):
        sync_remote(repo, tmp_path / "s")


def test_unreachable_remote_is_explained(tmp_path, allow_file_urls):
    repo = RepositoryRecord(repository_id="x", repo_url_or_path=f"file://{tmp_path}/missing.git", display_name="x")
    with pytest.raises(RemoteRepositoryError, match="git clone failed"):
        sync_remote(repo, tmp_path / "s")


# ------------------------------------------------------------------ fleet + jobs


@pytest.fixture
def remote_fleet(tmp_path, allow_file_urls):
    url_a, author_a = make_remote(tmp_path, "repo_a", "1.0.0")
    url_c, _ = make_remote(tmp_path, "repo_c", "2.0.0", source="int main(){return 0;}\n")
    (tmp_path / "fleet.yaml").write_text(textwrap.dedent(f"""\
        fleet_id: remote-fleet
        default_policy_id: p
        policies: {{p: {{allowed_change_levels: [major], major_approved: true}}}}
        repositories:
          - {{repository_id: repo_a, repo_url_or_path: "{url_a}", display_name: A}}
          - {{repository_id: repo_c, repo_url_or_path: "{url_c}", display_name: C}}
          - {{repository_id: gone, repo_url_or_path: "file://{tmp_path}/nope.git", display_name: G}}
        """))
    (tmp_path / "event.yaml").write_text(
        "event_id: toydep-v2\ndependency_name: toydep\nupstream_repo: x\nprevious_version: 1.0.0\ntarget_version: 2.0.0\n")
    return tmp_path, url_a, author_a


def test_selection_syncs_remotes_and_explains_failures(remote_fleet):
    root, url_a, _ = remote_fleet
    event, fleet = load_event(root / "event.yaml"), load_fleet(root / "fleet.yaml")
    sel = {s.repository.repository_id: s for s in select_repositories(
        event, fleet, DependencyIndex(root / ".depbot" / "inventory.json"), sources_dir=root / ".depbot" / "sources")}
    assert sel["repo_a"].eligible and sel["repo_a"].repo_root.startswith(str(root / ".depbot" / "sources"))
    assert sel["repo_a"].notes[0].startswith(f"cloned {url_a} (main @ ")
    assert sel["repo_c"].classification == FleetClassification.ALREADY_AT_TARGET_OR_NEWER
    assert sel["gone"].classification == FleetClassification.NEEDS_RESCAN and "clone failed" in sel["gone"].reason
    # Without a sources dir (e.g. old callers), remotes are refused rather than touched.
    plain = {s.repository.repository_id: s for s in select_repositories(event, fleet, DependencyIndex(root / "i.json"))}
    assert plain["repo_a"].classification == FleetClassification.NEEDS_RESCAN


def test_fleet_job_on_remote_repo_shares_history_and_leaves_remote_untouched(remote_fleet):
    root, url_a, author_a = remote_fleet
    remote_head = git(root / "remotes" / "repo_a.git", "rev-parse", "main")
    summary = run_fleet(load_event(root / "event.yaml"), load_fleet(root / "fleet.yaml"), stages=_stages(),
                        state_dir=root / ".depbot", artifacts_root=root / "artifacts")
    out = {o.repository_id: o for o in summary.outcomes}
    assert out["repo_a"].status == OutcomeStatus.VERIFIED, out["repo_a"].reason
    assert out["repo_c"].status == OutcomeStatus.SKIPPED and out["gone"].status == OutcomeStatus.BLOCKED
    import json
    state = json.loads(next((root / "artifacts").glob("toydep-v2-repo_a-*/state.json")).read_text())
    copy = state["repo_facts"]["working_copy"]
    assert copy["mode"] == "clone" and copy["baseline_commit"] == remote_head and copy["source_branch"] == "main"
    assert git(root / "remotes" / "repo_a.git", "rev-parse", "main") == remote_head  # nothing pushed
    assert git(root / "remotes" / "repo_a.git", "branch", "--list", "depbot/*") == ""


def test_upstream_commit_triggers_rescan(remote_fleet):
    root, _, author_a = remote_fleet
    event, fleet = load_event(root / "event.yaml"), load_fleet(root / "fleet.yaml")
    index_path, sources = root / ".depbot" / "inventory.json", root / ".depbot" / "sources"
    select_repositories(event, fleet, DependencyIndex(index_path), sources_dir=sources)
    (author_a / "conanfile.txt").write_text("[requires]\ntoydep/2.0.0\n")
    git(author_a, "commit", "-qam", "team upgraded by hand")
    git(author_a, "push", "-q", "origin", "HEAD:main")
    sel = {s.repository.repository_id: s for s in select_repositories(
        event, fleet, DependencyIndex(index_path), sources_dir=sources)}
    assert sel["repo_a"].rescanned and sel["repo_a"].classification == FleetClassification.ALREADY_AT_TARGET_OR_NEWER
    assert sel["repo_a"].notes[0].startswith("fetched ")


def test_cli_status_and_upgrade_accept_urls(remote_fleet, capsys):
    root, url_a, _ = remote_fleet
    assert main(["fleet", "status", "--fleet", str(root / "fleet.yaml")]) == 0
    out = capsys.readouterr().out
    assert "repo_a" in out and "toydep/1.0.0" in out and "clone failed" in out  # 'gone' row explained
    code = main(["upgrade", "--repo", url_a, "--dependency", "toydep", "--to", "2.0.0", "--approve-major",
                 "--state-dir", str(root / "cli-state"), "--artifacts", str(root / "cli-art")])
    out = capsys.readouterr().out
    assert out.startswith(f"synced {url_a} (main @ ") and code == 1  # real stages: no toolchain package here
    assert main(["upgrade", "--repo", "https://user:tok@github.com/a/b", "--dependency", "d", "--to", "1"]) == 2
    assert "must not embed credentials" in capsys.readouterr().err
