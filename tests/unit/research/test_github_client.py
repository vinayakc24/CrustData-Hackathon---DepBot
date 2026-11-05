from __future__ import annotations

import io
import json
import urllib.error

import pytest

from depbot.research.github_client import (
    DEFAULT_PER_PAGE,
    GitHubRelease,
    GitHubReleaseError,
    find_github_release,
    get_latest_github_release,
)


class Response(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class CountingResponse(Response):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


class FakeOpener:
    def __init__(self, responses=None, error=None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return self.responses.pop(0)


def _release(tag: str, **updates) -> dict:
    item = {
        "tag_name": tag,
        "name": f"Release {tag}",
        "body": "Exact\r\nrelease notes\r\n",
        "html_url": f"https://github.com/acme/widget/releases/tag/{tag}",
        "published_at": "2026-10-01T12:00:00Z",
        "prerelease": "-" in tag,
        "draft": False,
    }
    return item | updates


def _response(items) -> Response:
    return Response(json.dumps(items).encode("utf-8"))


def _mock(monkeypatch, opener: FakeOpener) -> None:
    monkeypatch.setattr("depbot.research.github_client.urllib.request.build_opener", lambda *args: opener)


@pytest.mark.parametrize(
    "repo",
    ["acme/widget", "https://github.com/acme/widget", "https://github.com/acme/widget.git"],
)
def test_supported_repository_formats(repo: str, monkeypatch) -> None:
    opener = FakeOpener([_response([_release("v2.0.0")])])
    _mock(monkeypatch, opener)
    assert find_github_release(upstream_repo=repo, target_version="2.0.0").tag_name == "v2.0.0"
    assert opener.requests[0].full_url.startswith("https://api.github.com/repos/acme/widget/releases?")


@pytest.mark.parametrize(
    "repo",
    ["", "acme", "acme/widget/extra", "../widget", "acme/..", "http://github.com/acme/widget",
     "https://evil.example/acme/widget", "https://user:pw@github.com/acme/widget", "acme/widget?x=1"],
)
def test_rejects_invalid_repository_identifiers(repo: str) -> None:
    with pytest.raises(GitHubReleaseError):
        find_github_release(upstream_repo=repo, target_version="2.0.0")


def test_matches_exact_version_and_preserves_release_evidence(monkeypatch) -> None:
    opener = FakeOpener([_response([_release("v2.0.0"), _release("v2.0.0-rc1"), _release("12.0.0")])])
    _mock(monkeypatch, opener)
    found = find_github_release(upstream_repo="acme/widget", target_version="2.0.0")
    assert found == GitHubRelease(
        "v2.0.0", "Release v2.0.0", "Exact\r\nrelease notes\r\n",
        "https://github.com/acme/widget/releases/tag/v2.0.0", "2026-10-01T12:00:00Z", False,
    )


def test_optional_v_is_normalized_but_prerelease_is_not(monkeypatch) -> None:
    opener = FakeOpener([_response([_release("2.0.0"), _release("2.0.0-rc1")])])
    _mock(monkeypatch, opener)
    assert find_github_release(upstream_repo="acme/widget", target_version="v2.0.0").tag_name == "2.0.0"


def test_missing_release_is_explicit(monkeypatch) -> None:
    _mock(monkeypatch, FakeOpener([_response([_release("1.0.0")])]))
    with pytest.raises(GitHubReleaseError, match="no release"):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0")


def test_duplicate_matching_tags_are_rejected(monkeypatch) -> None:
    _mock(monkeypatch, FakeOpener([_response([_release("2.0.0"), _release("v2.0.0")])]))
    with pytest.raises(GitHubReleaseError, match="multiple releases"):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0")


def test_paginates_until_short_page_and_applies_timeout(monkeypatch) -> None:
    first_page = [_release(f"1.0.{i}") for i in range(DEFAULT_PER_PAGE)]
    opener = FakeOpener([_response(first_page), _response([_release("v2.0.0")])])
    _mock(monkeypatch, opener)
    result = find_github_release(upstream_repo="acme/widget", target_version="2.0.0", timeout_seconds=4)
    assert result.tag_name == "v2.0.0"
    assert len(opener.requests) == 2
    assert "page=1" in opener.requests[0].full_url and "page=2" in opener.requests[1].full_url
    assert opener.timeouts == [4, 4]


def test_page_limit_is_enforced(monkeypatch) -> None:
    full = _response([_release(f"1.0.{i}") for i in range(DEFAULT_PER_PAGE)])
    opener = FakeOpener([full])
    _mock(monkeypatch, opener)
    with pytest.raises(GitHubReleaseError, match="page limit"):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0", max_pages=1)


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://api.github.com", 404, "missing", {}, None),
    urllib.error.HTTPError("https://api.github.com", 403, "rate", {"X-RateLimit-Remaining": "0"}, None),
    urllib.error.HTTPError("https://api.github.com", 429, "rate", {}, None),
    urllib.error.URLError("network unavailable"),
    TimeoutError("timed out"),
])
def test_http_rate_network_and_timeout_errors(monkeypatch, error: Exception) -> None:
    _mock(monkeypatch, FakeOpener(error=error))
    with pytest.raises(GitHubReleaseError):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0")


@pytest.mark.parametrize("body", [b"not json", b"\xff"])
def test_malformed_or_non_utf8_response(monkeypatch, body: bytes) -> None:
    _mock(monkeypatch, FakeOpener([Response(body)]))
    with pytest.raises(GitHubReleaseError, match="invalid JSON"):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0")


def test_missing_required_release_field_is_reported(monkeypatch) -> None:
    malformed = _release("2.0.0")
    del malformed["html_url"]
    _mock(monkeypatch, FakeOpener([_response([malformed])]))
    with pytest.raises(GitHubReleaseError, match="html_url"):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0")


def test_response_limit_is_enforced_during_read(monkeypatch) -> None:
    response = CountingResponse(json.dumps([_release("2.0.0")]).encode() + b" " * 100)
    _mock(monkeypatch, FakeOpener([response]))
    with pytest.raises(GitHubReleaseError, match="byte limit"):
        find_github_release(upstream_repo="acme/widget", target_version="2.0.0", max_response_bytes=10)
    assert response.bytes_read == 11


def test_latest_release_uses_bounded_latest_endpoint_and_preserves_fields(monkeypatch) -> None:
    item = _release("v3.0.0") | {"body": "Latest\r\nnotes", "html_url": "https://github.com/acme/widget/releases/tag/v3.0.0"}
    opener = FakeOpener([Response(json.dumps(item).encode("utf-8"))])
    _mock(monkeypatch, opener)

    release = get_latest_github_release(upstream_repo="https://github.com/acme/widget.git", timeout_seconds=3)

    assert release == GitHubRelease(
        "v3.0.0", "Release v3.0.0", "Latest\r\nnotes",
        "https://github.com/acme/widget/releases/tag/v3.0.0", "2026-10-01T12:00:00Z", False,
    )
    assert opener.requests[0].full_url == "https://api.github.com/repos/acme/widget/releases/latest"
    assert opener.timeouts == [3]


@pytest.mark.parametrize("updates, message", [
    ({"prerelease": True}, "prerelease"),
    ({"draft": True}, "draft"),
    ({"draft": None}, "draft flag"),
])
def test_latest_release_rejects_prereleases_and_drafts(monkeypatch, updates, message: str) -> None:
    _mock(monkeypatch, FakeOpener([Response(json.dumps(_release("v2.0.0") | updates).encode())]))

    with pytest.raises(GitHubReleaseError, match=message):
        get_latest_github_release(upstream_repo="acme/widget")


def test_latest_release_rejects_invalid_repositories_and_bounded_payloads(monkeypatch) -> None:
    with pytest.raises(GitHubReleaseError):
        get_latest_github_release(upstream_repo="https://evil.example/acme/widget")

    response = CountingResponse(json.dumps(_release("v2.0.0")).encode() + b" " * 100)
    _mock(monkeypatch, FakeOpener([response]))
    with pytest.raises(GitHubReleaseError, match="byte limit"):
        get_latest_github_release(upstream_repo="acme/widget", max_response_bytes=10)
    assert response.bytes_read == 11


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://api.github.com", 404, "missing", {}, None),
    urllib.error.HTTPError("https://api.github.com", 403, "rate", {"X-RateLimit-Remaining": "0"}, None),
    urllib.error.HTTPError("https://api.github.com", 429, "rate", {}, None),
    urllib.error.URLError("network unavailable"),
    TimeoutError("timed out"),
])
def test_latest_release_reports_http_rate_network_and_timeout_errors(monkeypatch, error: Exception) -> None:
    _mock(monkeypatch, FakeOpener(error=error))

    with pytest.raises(GitHubReleaseError):
        get_latest_github_release(upstream_repo="acme/widget")


@pytest.mark.parametrize("body", [b"not json", b"[]", b"\xff"])
def test_latest_release_rejects_malformed_json_or_shape(monkeypatch, body: bytes) -> None:
    _mock(monkeypatch, FakeOpener([Response(body)]))

    with pytest.raises(GitHubReleaseError):
        get_latest_github_release(upstream_repo="acme/widget")
