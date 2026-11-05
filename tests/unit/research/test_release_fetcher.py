from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from depbot.research.release_fetcher import (
    MAX_REDIRECTS,
    ReleaseDocument,
    ReleaseFetchError,
    _SafeRedirectHandler,
    fetch_release_document,
)


class Response(io.BytesIO):
    def __init__(self, content: bytes, url: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        super().__init__(content)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.url = None
        self.timeout = None

    def open(self, url, timeout):
        self.url, self.timeout = url, timeout
        if self.error:
            raise self.error
        return self.response


class CountingResponse(Response):
    def __init__(self, content: bytes, url: str) -> None:
        super().__init__(content, url)
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def _mock_opener(monkeypatch, opener: FakeOpener, handler_capture: list | None = None) -> None:
    def build(*handlers):
        if handler_capture is not None:
            handler_capture.extend(handlers)
        return opener

    monkeypatch.setattr("depbot.research.release_fetcher.urllib.request.build_opener", build)


def test_local_retrieval_preserves_text_and_file_uri(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    text = "## 2.0.0\r\nRemoved API\r\n"
    path.write_bytes(text.encode("utf-8"))
    result = fetch_release_document(local_path=path)
    assert result == ReleaseDocument(path.resolve().as_uri(), text, "text/markdown")


def test_https_retrieval_uses_final_canonical_url_and_timeout(monkeypatch) -> None:
    response = Response(b"# Release\r\nExact notes\r\n", "https://EXAMPLE.com:443/releases/v2#fragment")
    opener = FakeOpener(response)
    _mock_opener(monkeypatch, opener)
    result = fetch_release_document(source_url="https://Example.COM/releases/v2#old", timeout_seconds=3.5)
    assert result.source_url == "https://example.com/releases/v2"
    assert result.content == "# Release\r\nExact notes\r\n"
    assert result.content_type == "text/markdown"
    assert opener.timeout == 3.5


@pytest.mark.parametrize("kwargs", [{}, {"source_url": "https://example.com", "local_path": Path("file.md")}])
def test_requires_exactly_one_source(kwargs) -> None:
    with pytest.raises(ReleaseFetchError, match="exactly one"):
        fetch_release_document(**kwargs)


def test_local_missing_directory_invalid_utf8_and_size_are_errors(tmp_path: Path) -> None:
    with pytest.raises(ReleaseFetchError):
        fetch_release_document(local_path=tmp_path / "missing.md")
    with pytest.raises(ReleaseFetchError, match="regular file"):
        fetch_release_document(local_path=tmp_path)
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff")
    with pytest.raises(ReleaseFetchError, match="UTF-8"):
        fetch_release_document(local_path=bad)
    large = tmp_path / "large.md"
    large.write_text("123456", encoding="utf-8")
    with pytest.raises(ReleaseFetchError, match="byte limit"):
        fetch_release_document(local_path=large, max_bytes=5)


@pytest.mark.parametrize("url", ["not a URL", "http://example.com/notes", "https:///missing", "https://user:pass@example.com/notes", "https://example.com:bad/"])
def test_rejects_invalid_or_unsafe_source_urls(url: str) -> None:
    with pytest.raises(ReleaseFetchError):
        fetch_release_document(source_url=url)


def test_remote_response_size_is_limited_while_reading(monkeypatch) -> None:
    response = CountingResponse(b"x" * 100, "https://example.com/notes")
    _mock_opener(monkeypatch, FakeOpener(response))
    with pytest.raises(ReleaseFetchError, match="byte limit"):
        fetch_release_document(source_url="https://example.com/notes", max_bytes=8)
    assert response.bytes_read == 9


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"),
    urllib.error.HTTPError("https://example.com", 503, "unavailable", {}, None),
    urllib.error.URLError("DNS failure"),
])
def test_timeout_http_and_network_errors_are_wrapped(monkeypatch, error: Exception) -> None:
    _mock_opener(monkeypatch, FakeOpener(error=error))
    with pytest.raises(ReleaseFetchError):
        fetch_release_document(source_url="https://example.com/notes")


def test_remote_invalid_utf8_is_reported(monkeypatch) -> None:
    _mock_opener(monkeypatch, FakeOpener(Response(b"\xff", "https://example.com/notes")))
    with pytest.raises(ReleaseFetchError, match="UTF-8"):
        fetch_release_document(source_url="https://example.com/notes")


def test_safe_redirect_is_https_and_canonical() -> None:
    handler = _SafeRedirectHandler(MAX_REDIRECTS)
    request = urllib.request.Request("https://example.com/start")
    redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://example.org/end")
    assert redirected.full_url == "https://example.org/end"
    assert handler.redirects == 1


@pytest.mark.parametrize("destination", ["http://example.org/downgrade", "https://user:pw@example.org/secret"])
def test_rejects_unsafe_redirect_destinations(destination: str) -> None:
    handler = _SafeRedirectHandler(MAX_REDIRECTS)
    request = urllib.request.Request("https://example.com/start")
    with pytest.raises(ReleaseFetchError):
        handler.redirect_request(request, None, 302, "Found", {}, destination)


def test_redirect_limit_is_enforced() -> None:
    handler = _SafeRedirectHandler(1)
    request = urllib.request.Request("https://example.com/start")
    handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/one")
    with pytest.raises(ReleaseFetchError, match="redirects"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/two")
