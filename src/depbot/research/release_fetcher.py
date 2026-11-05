"""Bounded retrieval of explicitly selected local or HTTPS release documents."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 1_000_000
MAX_REDIRECTS = 5
_READ_CHUNK_BYTES = 64 * 1024


class ReleaseFetchError(RuntimeError):
    """Raised when a release document cannot be retrieved safely."""


@dataclass(frozen=True)
class ReleaseDocument:
    source_url: str
    content: str
    content_type: str


def _validated_https_url(url: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url)
        # Accessing .port validates malformed port strings and range.
        _ = parts.port
    except ValueError as exc:
        raise ReleaseFetchError(f"invalid source URL: {url!r}") from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ReleaseFetchError("remote release documents require an HTTPS URL with a hostname")
    if parts.username is not None or parts.password is not None:
        raise ReleaseFetchError("credentials are forbidden in release document URLs")
    return parts


def _canonical_url(url: str) -> str:
    parts = _validated_https_url(url)
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parts.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urllib.parse.urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects: int) -> None:
        super().__init__()
        self.max_redirects = max_redirects
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > self.max_redirects:
            raise ReleaseFetchError(f"release document exceeded {self.max_redirects} redirects")
        _validated_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_bounded(stream, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = stream.read(min(_READ_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ReleaseFetchError(f"release document exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_release_document(
    *,
    source_url: str | None = None,
    local_path: Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ReleaseDocument:
    """Retrieve exact UTF-8 text from one explicit local path or HTTPS URL."""
    if (source_url is None) == (local_path is None):
        raise ReleaseFetchError("provide exactly one of source_url or local_path")
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ReleaseFetchError("timeout_seconds and max_bytes must be positive")

    if local_path is not None:
        try:
            path = Path(local_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ReleaseFetchError(f"local release document is unavailable: {local_path}") from exc
        if not path.is_file():
            raise ReleaseFetchError(f"local release document is not a regular file: {path}")
        try:
            with path.open("rb") as stream:
                raw = _read_bounded(stream, max_bytes)
        except ReleaseFetchError:
            raise
        except OSError as exc:
            raise ReleaseFetchError(f"could not read local release document {path}: {exc}") from exc
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseFetchError(f"local release document is not valid UTF-8: {path}") from exc
        return ReleaseDocument(path.as_uri(), content, "text/markdown")

    assert source_url is not None
    canonical = _canonical_url(source_url)
    redirect_handler = _SafeRedirectHandler(MAX_REDIRECTS)
    opener = urllib.request.build_opener(redirect_handler)
    try:
        with opener.open(canonical, timeout=timeout_seconds) as response:
            final_url = response.geturl()
            final_canonical = _canonical_url(final_url)
            raw = _read_bounded(response, max_bytes)
            content_type = response.headers.get("Content-Type", "text/plain").split(";", 1)[0].strip().lower()
    except ReleaseFetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise ReleaseFetchError(f"HTTP error retrieving release document: {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReleaseFetchError(f"network failure retrieving release document: {exc}") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseFetchError("remote release document is not valid UTF-8") from exc
    return ReleaseDocument(final_canonical, content, content_type or "text/plain")
