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


def list_version_sections(changelog_text: str) -> list[ChangelogSection]:
    """Return recognized version sections in source order, preserving their text."""
    if not isinstance(changelog_text, str):
        raise TypeError("changelog_text must be a string")
    lines = changelog_text.splitlines(keepends=True)
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        parsed = _heading_version(line)
        if parsed is None:
            continue
        version, heading = parsed
        headings.append((index, version, heading))
    sections: list[ChangelogSection] = []
    for heading_index, (index, version, heading) in enumerate(headings):
        next_index = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(lines)
        content_lines = lines[index + 1:next_index]
        sections.append(ChangelogSection(
            version=version,
            heading=heading,
            content="".join(content_lines),
            line_start=index + 1,
            line_end=next_index,
        ))
    return sections


def find_version_section(changelog_text: str, target_version: str) -> ChangelogSection | None:
    """Extract the exact requested Markdown version section, preserving text."""
    if not isinstance(target_version, str):
        raise TypeError("target_version must be a string")
    target = _normalize(target_version)
    if not target:
        raise ValueError("target_version must not be empty")
    matches = [section for section in list_version_sections(changelog_text)
               if _normalize(section.version) == target]
    if len(matches) > 1:
        raise ChangelogParseError(f"duplicate changelog sections for version {target_version!r}")
    return matches[0] if matches else None
