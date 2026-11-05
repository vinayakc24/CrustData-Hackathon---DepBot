"""Bounded GitHub REST release lookup for an explicitly identified repository."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PAGES = 5
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class GitHubReleaseError(RuntimeError):
    """Raised when a target release cannot be retrieved unambiguously."""


@dataclass(frozen=True)
class GitHubRelease:
    tag_name: str
    name: str | None
    body: str | None
    html_url: str
    published_at: str | None
    prerelease: bool


def _repository_path(upstream_repo: str) -> str:
    value = upstream_repo.strip()
    if not value:
        raise GitHubReleaseError("upstream_repo must identify a GitHub repository")
    if "://" in value:
        try:
            parsed = urllib.parse.urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise GitHubReleaseError("malformed GitHub repository URL") from exc
        if parsed.scheme.lower() != "https" or parsed.hostname is None or parsed.hostname.lower() != "github.com":
            raise GitHubReleaseError("repository URL must use https://github.com")
        if parsed.username is not None or parsed.password is not None:
            raise GitHubReleaseError("credentials are forbidden in repository URLs")
        if parsed.port not in (None, 443) or parsed.query or parsed.fragment:
            raise GitHubReleaseError("repository URL must not include a port, query, or fragment")
        value = parsed.path.strip("/")
        if value.endswith(".git"):
            value = value[:-4]
    parts = value.split("/")
    if len(parts) != 2 or any(not part or part in (".", "..") for part in parts):
        raise GitHubReleaseError("upstream_repo must be owner/repo or a GitHub repository URL")
    if not all(_REPOSITORY_NAME.fullmatch(part) for part in parts):
        raise GitHubReleaseError("repository identifier contains unsafe path characters")
    return "/".join(parts)


def _normalize_version(version: str) -> str:
    return version[1:] if version.startswith("v") else version


def _read_response(response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise GitHubReleaseError(f"GitHub API response exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _api_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "DepBot-release-research"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_release(item: object) -> GitHubRelease:
    if not isinstance(item, dict):
        raise GitHubReleaseError("GitHub API returned a malformed release entry")
    for key in ("tag_name", "name", "body", "html_url", "published_at", "prerelease"):
        if key not in item:
            raise GitHubReleaseError(f"GitHub API release is missing required field {key!r}")
    tag = item["tag_name"]
    name, body, url, published, prerelease = (
        item["name"], item["body"], item["html_url"], item["published_at"], item["prerelease"]
    )
    if not isinstance(tag, str) or not tag or (name is not None and not isinstance(name, str)):
        raise GitHubReleaseError("GitHub API release has invalid tag or name")
    if body is not None and not isinstance(body, str):
        raise GitHubReleaseError("GitHub API release body is not text")
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise GitHubReleaseError("GitHub API release has an invalid html_url")
    if published is not None and not isinstance(published, str):
        raise GitHubReleaseError("GitHub API release has invalid published_at")
    if not isinstance(prerelease, bool):
        raise GitHubReleaseError("GitHub API release has invalid prerelease flag")
    return GitHubRelease(tag, name, body, url, published, prerelease)


def get_latest_github_release(
    *,
    upstream_repo: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> GitHubRelease:
    """Retrieve GitHub's latest published stable release for an explicit repo."""
    repo = _repository_path(upstream_repo)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes <= 0
    ):
        raise GitHubReleaseError("timeout_seconds and max_response_bytes must be positive and finite")

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers=_api_headers(),
    )
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = _read_response(response, max_response_bytes)
    except GitHubReleaseError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise GitHubReleaseError(f"no published stable release found for {repo}") from exc
        if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
            raise GitHubReleaseError("GitHub API rate limit exceeded") from exc
        raise GitHubReleaseError(f"GitHub API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GitHubReleaseError(f"GitHub API network failure: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubReleaseError("GitHub API returned invalid JSON or UTF-8") from exc
    if not isinstance(payload, dict):
        raise GitHubReleaseError("GitHub API latest-release response must be a JSON object")
    draft = payload.get("draft")
    if not isinstance(draft, bool):
        raise GitHubReleaseError("GitHub API release is missing a valid draft flag")
    release = _parse_release(payload)
    if draft or release.prerelease:
        raise GitHubReleaseError("GitHub latest-release endpoint returned a draft or prerelease")
    return release


def find_github_release(
    *,
    upstream_repo: str,
    target_version: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> GitHubRelease:
    """Find one release whose tag exactly matches target_version (optional v)."""
    repo = _repository_path(upstream_repo)
    if not isinstance(target_version, str) or not target_version:
        raise GitHubReleaseError("target_version must be a non-empty string")
    if timeout_seconds <= 0 or max_response_bytes <= 0 or max_pages <= 0:
        raise GitHubReleaseError("timeout_seconds, max_response_bytes, and max_pages must be positive")
    target = _normalize_version(target_version)
    matches: list[GitHubRelease] = []
    opener = urllib.request.build_opener()
    for page in range(1, max_pages + 1):
        url = f"https://api.github.com/repos/{repo}/releases?per_page={DEFAULT_PER_PAGE}&page={page}"
        request = urllib.request.Request(
            url,
            headers=_api_headers(),
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                raw = _read_response(response, max_response_bytes)
        except GitHubReleaseError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
                raise GitHubReleaseError("GitHub API rate limit exceeded") from exc
            raise GitHubReleaseError(f"GitHub API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubReleaseError(f"GitHub API network failure: {exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubReleaseError("GitHub API returned invalid JSON or UTF-8") from exc
        if not isinstance(payload, list):
            raise GitHubReleaseError("GitHub API release response must be a JSON list")
        page_releases = [_parse_release(entry) for entry in payload]
        matches.extend(release for release in page_releases if _normalize_version(release.tag_name) == target)
        if len(payload) < DEFAULT_PER_PAGE:
            break
    else:
        raise GitHubReleaseError(f"release lookup reached the {max_pages}-page limit")
    if not matches:
        raise GitHubReleaseError(f"no release found for {repo} at version {target_version!r}")
    if len(matches) > 1:
        raise GitHubReleaseError(f"multiple releases match version {target_version!r} in {repo}")
    return matches[0]
