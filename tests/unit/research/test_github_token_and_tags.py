"""Optional read-only GitHub token, exact-tag lookup and monitor tag prefixes."""

import io
import json

import pytest

import depbot.monitor.release_monitor as rm
import depbot.research.github_client as gc
from depbot.monitor.release_monitor import ReleaseMonitorError, ReleaseWatch, poll_release_watch
from depbot.research.github_client import GitHubRelease


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _release(tag):
    return {"tag_name": tag, "name": tag, "body": f"notes {tag}", "html_url": f"https://github.com/a/b/releases/tag/{tag}",
            "published_at": None, "prerelease": False, "draft": False}


@pytest.fixture
def fake_api(monkeypatch):
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            body = [_release("fmt-11.0.2"), _release("v10.2.1")] if request.full_url.endswith("page=1") else _release("v2.0.0")
            return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(gc.urllib.request, "build_opener", lambda *a: Opener())
    return seen


def test_token_is_sent_only_when_configured_and_never_redirected(fake_api, monkeypatch):
    monkeypatch.delenv(gc.TOKEN_ENV_VAR, raising=False)
    gc.get_latest_github_release(upstream_repo="a/b")
    assert not fake_api[-1].has_header("Authorization") and not fake_api[-1].unredirected_hdrs
    monkeypatch.setenv(gc.TOKEN_ENV_VAR, "ghp_readonly")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_publishing_must_not_be_used")
    gc.get_latest_github_release(upstream_repo="a/b")
    request = fake_api[-1]
    assert request.unredirected_hdrs == {"Authorization": "Bearer ghp_readonly"}
    assert "Authorization" not in request.headers  # would otherwise follow redirects


def test_exact_tag_lookup_finds_prefixed_tags(fake_api):
    assert gc.find_github_release(upstream_repo="a/b", target_version="11.0.2", exact_tag="fmt-11.0.2").tag_name == "fmt-11.0.2"
    assert gc.find_github_release(upstream_repo="a/b", target_version="10.2.1").tag_name == "v10.2.1"
    with pytest.raises(gc.GitHubReleaseError, match="no release found"):
        gc.find_github_release(upstream_repo="a/b", target_version="11.0.2")  # prefix not stripped implicitly


def _latest(tag):
    return lambda **kw: GitHubRelease(tag_name=tag, name=tag, body="", html_url=f"https://github.com/fmtlib/fmt/releases/tag/{tag}",
                                      published_at=None, prerelease=False)


def test_monitor_strips_configured_tag_prefix(monkeypatch):
    monkeypatch.setattr(rm, "get_latest_github_release", _latest("fmt-11.0.2"))
    event = poll_release_watch(ReleaseWatch("fmt", "fmtlib/fmt", "10.2.1", tag_prefix="fmt-"))
    assert (event.previous_version, event.target_version) == ("10.2.1", "11.0.2")
    assert event.event_id == "github-release:fmtlib/fmt:fmt-11.0.2" and event.source_url.endswith("/fmt-11.0.2")
    with pytest.raises(ReleaseMonitorError, match="cannot compare"):
        poll_release_watch(ReleaseWatch("fmt", "fmtlib/fmt", "10.2.1"))  # no prefix configured
    with pytest.raises(ReleaseMonitorError, match="does not start with"):
        poll_release_watch(ReleaseWatch("fmt", "fmtlib/fmt", "10.2.1", tag_prefix="release-"))
