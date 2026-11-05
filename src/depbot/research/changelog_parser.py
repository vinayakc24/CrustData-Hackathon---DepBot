"""Deterministic extraction of a version's section from Markdown changelogs."""

from __future__ import annotations

import re
from dataclasses import dataclass


_HEADING = re.compile(r"^#{1,2}[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$")
_VERSION = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")


class ChangelogParseError(ValueError):
    """Raised when changelog structure is ambiguous or malformed."""


@dataclass(frozen=True)
class ChangelogSection:
    version: str
    heading: str
    content: str
    line_start: int
    line_end: int


def _heading_version(line: str) -> tuple[str, str] | None:
    match = _HEADING.match(line)
    if not match:
        return None
    heading = line.rstrip("\r\n")
    title = match.group(1).strip()
    if title.startswith("["):
        close = title.find("]")
        if close < 0:
            return None
        version = title[1:close].strip()
        suffix = title[close + 1:].strip()
        if suffix and not suffix.startswith(("-", "—", "–")):
            return None
    else:
        version = title.split(None, 1)[0].strip()
        # A date or descriptive suffix is allowed only after a spaced dash.
        remainder = title[len(version):].strip()
        if remainder and not remainder.startswith(("-", "—", "–")):
            return None
    normalized = version[1:] if version.startswith("v") else version
    if not _VERSION.fullmatch(version) and not _VERSION.fullmatch(normalized):
        return None
    return version, heading


def _normalize(version: str) -> str:
    value = version.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    return value[1:] if value.startswith("v") else value


def find_version_section(changelog_text: str, target_version: str) -> ChangelogSection | None:
    """Extract the exact requested Markdown version section, preserving text."""
    if not isinstance(changelog_text, str) or not isinstance(target_version, str):
        raise TypeError("changelog_text and target_version must be strings")
    target = _normalize(target_version)
    if not target:
        raise ValueError("target_version must not be empty")
    lines = changelog_text.splitlines(keepends=True)
    matches: list[tuple[int, str, str]] = []
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        parsed = _heading_version(line)
        if parsed is None:
            continue
        version, heading = parsed
        normalized = _normalize(version)
        headings.append((index, version, heading))
        if normalized == target:
            matches.append((index, version, heading))
    if len(matches) > 1:
        raise ChangelogParseError(f"duplicate changelog sections for version {target_version!r}")
    if not matches:
        return None
    index, version, heading = matches[0]
    next_index = next((item[0] for item in headings if item[0] > index), len(lines))
    content_lines = lines[index + 1:next_index]
    end_line = index + len(content_lines) + 1
    return ChangelogSection(
        version=version,
        heading=heading,
        content="".join(content_lines),
        line_start=index + 1,
        line_end=end_line,
    )


def find_sections_in_range(
    changelog_text: str, *, after_version: str | None, through_version: str
) -> list[ChangelogSection]:
    """All version sections with ``after_version < version <= through_version``, in file order.

    A 1.0 → 3.0 upgrade must see the breaking changes documented under 2.0 as well.
    Headings whose versions cannot be compared are skipped; duplicates raise.
    """
    from depbot.fleet.versions import UnparseableVersion, compare

    lines = changelog_text.splitlines(keepends=True)
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        parsed = _heading_version(line)
        if parsed is not None:
            headings.append((index, parsed[0], parsed[1]))
    seen: set[str] = set()
    sections: list[ChangelogSection] = []
    for position, (index, version, heading) in enumerate(headings):
        normalized = _normalize(version)
        try:
            if compare(normalized, _normalize(through_version)) > 0:
                continue
            if after_version and compare(normalized, _normalize(after_version)) <= 0:
                continue
        except UnparseableVersion:
            continue
        if normalized in seen:
            raise ChangelogParseError(f"duplicate changelog sections for version {version!r}")
        seen.add(normalized)
        next_index = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        content = "".join(lines[index + 1:next_index])
        sections.append(ChangelogSection(version=version, heading=heading, content=content,
                                         line_start=index + 1, line_end=next_index))
    return sections
