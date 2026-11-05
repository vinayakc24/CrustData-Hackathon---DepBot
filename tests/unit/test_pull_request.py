"""PR delivery: local artifact is real; GitHub (`git push`, `gh`) is mocked."""

import subprocess

import pytest

import depbot.orchestrator as orch
from depbot.models import OutcomeStatus, UpgradePolicy
from depbot.orchestrator import run_repository_job
from depbot.reporting.pull_request import prepare_pull_request, publish_pull_request
from depbot.state import JobStatus
from tests.unit.test_orchestrator import CHANGE, _stages, job  # noqa: F401  (fixture)


class FakeRunner:
    def __init__(self, remote="https://github.com/acme/repo_a.git", push_rc=0, gh_rc=0):
        self.calls, self.remote, self.push_rc, self.gh_rc = [], remote, push_rc, gh_rc

    def __call__(self, args, **kw):
        assert kw["shell"] is False and kw["timeout"]
        self.calls.append(args)
        if args[:3] == ["git", "remote", "get-url"]:
            ok = self.remote is not None
            return subprocess.CompletedProcess(args, 0 if ok else 2, self.remote or "", "" if ok else "no origin")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, self.push_rc, "", "denied" if self.push_rc else "")
        if args[:4] == ["gh", "api", "--method", "POST"]:
            out = "https://github.com/acme/repo_a/pull/7\n" if self.gh_rc == 0 else ""
            return subprocess.CompletedProcess(args, self.gh_rc, out, "" if self.gh_rc == 0 else "gh: no auth")
        raise AssertionError(f"unexpected command {args}")


def _git_init_commit(path):
    run = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                    cwd=path, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("commit", "-qm", "init")


def test_verified_job_writes_evidence_backed_local_pr_artifact(job, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_never_appear")
    state, outcome = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.VERIFIED and outcome.pr_url is None
    body = (tmp_path / "a" / state.run_id / "pr" / "pr-body.md").read_text()
    assert "Local PR artifact" in body and "has not been published" in body
    assert CHANGE.id in body and "file://CHANGELOG.md" in body
    assert "`src/main.cpp:2` `old_sum`" in body
    assert "| install | `install` | 0 |" in body and "Tests: 1 passed, 0 failed." in body
    assert "auto-merge is disabled" in body and "ghp_" not in body
    assert state.repo_facts["pull_request"]["published"] is False


def test_pre_pr_gate_blocks_artifact_without_tests(job, tmp_path):
    state, _ = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    state.verification = state.verification.model_copy(update={"tests": None})
    assert prepare_pull_request(state, UpgradePolicy(), tmp_path / "x", base_branch="main") is None


def test_publish_pushes_branch_and_opens_pr_without_merge(job, tmp_path):
    state, _ = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    draft = prepare_pull_request(state, job.policy, tmp_path / "a" / state.run_id, base_branch="main")
    fake = FakeRunner()
    result = publish_pull_request(draft, state, run=fake)
    assert result.published and result.url == "https://github.com/acme/repo_a/pull/7"
    push, create = fake.calls[1], fake.calls[2]
    assert push == ["git", "push", "https://github.com/acme/repo_a.git", "HEAD:refs/heads/depbot/toydep-2.0.0"]
    assert create[:5] == ["gh", "api", "--method", "POST", "repos/acme/repo_a/pulls"]
    assert "head=depbot/toydep-2.0.0" in create and "base=main" in create
    assert any(a.startswith("body=@") and a.endswith("pr-body.md") for a in create)
    assert not any("merge" in a for call in fake.calls for a in call)


@pytest.mark.parametrize("runner,note", [
    (FakeRunner(remote=None), "no GitHub 'origin' remote"),
    (FakeRunner(remote="/local/path/repo"), "no GitHub 'origin' remote"),
    (FakeRunner(push_rc=1), "git push failed"),
    (FakeRunner(gh_rc=1), "PR creation failed"),
])
def test_publish_failures_are_explained(job, tmp_path, runner, note):
    state, _ = run_repository_job(job, _stages(), artifacts_root=tmp_path / "a")
    draft = prepare_pull_request(state, job.policy, tmp_path / "a" / state.run_id, base_branch="main")
    result = publish_pull_request(draft, state, run=runner)
    assert not result.published and note in result.publish_note


def test_create_pr_on_snapshot_copy_reports_failed_pr_but_verified(job, tmp_path):
    job2 = job.model_copy(update={"create_pr": True})
    state, outcome = run_repository_job(job2, _stages(), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.FAILED_PR and "snapshot" in state.status_reason
    assert outcome.status == OutcomeStatus.VERIFIED and outcome.verification_passed


def test_create_pr_on_git_repo_opens_pr(job, tmp_path, monkeypatch):
    _git_init_commit(tmp_path / "repo_a")
    fake = FakeRunner()
    monkeypatch.setattr(orch, "publish_pull_request", lambda d, s: publish_pull_request(d, s, run=fake))
    job2 = job.model_copy(update={"create_pr": True})
    state, outcome = run_repository_job(job2, _stages(), artifacts_root=tmp_path / "a")
    assert state.status == JobStatus.PR_CREATED, state.status_reason
    assert outcome.status == OutcomeStatus.VERIFIED and outcome.pr_url.endswith("/pull/7")
    assert state.repo_facts["working_copy"]["mode"] == "clone"
    assert "base=main" in fake.calls[-1]
