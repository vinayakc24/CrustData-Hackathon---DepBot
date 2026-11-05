"""Small, dependency-free version comparison for package version strings."""

from __future__ import annotations

import re

from depbot.models import ChangeLevel

_NUMERIC = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+].*)?$")


class UnparseableVersion(ValueError):
    pass


def parse_version(text: str) -> tuple[int, int, int]:
    match = _NUMERIC.match(text.strip())
    if match is None:
        raise UnparseableVersion(f"cannot compare version {text!r}")
    parts = [int(p) for p in match.group(1).split(".")][:3]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def compare(a: str, b: str) -> int:
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def change_level(current: str, target: str) -> ChangeLevel:
    try:
        c, t = parse_version(current), parse_version(target)
    except UnparseableVersion:
        return ChangeLevel.UNKNOWN
    if t <= c:
        return ChangeLevel.NONE
    if t[0] != c[0]:
        return ChangeLevel.MAJOR
    if t[1] != c[1]:
        return ChangeLevel.MINOR
    return ChangeLevel.PATCH
