"""Local JSONL storage for verified migration patterns.

Patterns are searchable context only. This module never applies stored patches.
"""

from __future__ import annotations

import json
from pathlib import Path

from depbot.models import MigrationPattern, MigrationVerification


class KnowledgeStoreError(RuntimeError):
    """The knowledge file is invalid or a pattern is not eligible for storage."""


def store_verified_pattern(path: Path, pattern: MigrationPattern) -> None:
    """Append one verified, source-backed pattern; reject unverified outcomes."""
    if pattern.verification_status != MigrationVerification.VERIFIED:
        raise KnowledgeStoreError("only verified migration patterns may be stored")
    if not pattern.evidence_urls or not pattern.source_run_id:
        raise KnowledgeStoreError("verified patterns require source evidence and a source run id")
    existing = find_verified_patterns(path, dependency_name=pattern.dependency_name)
    if any(item.pattern_id == pattern.pattern_id for item in existing):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(pattern.model_dump_json() + "\n")


def find_verified_patterns(
    path: Path,
    *,
    dependency_name: str,
    old_symbol: str | None = None,
    version_range: str | None = None,
) -> list[MigrationPattern]:
    """Retrieve only verified patterns matching exact dependency/symbol filters."""
    if not path.is_file():
        return []
    matches: list[MigrationPattern] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            pattern = MigrationPattern.model_validate_json(line)
            if pattern.verification_status != MigrationVerification.VERIFIED:
                continue
            if pattern.dependency_name != dependency_name:
                continue
            if version_range is not None and pattern.version_range != version_range:
                continue
            if old_symbol is not None and old_symbol not in pattern.old_symbols:
                continue
            matches.append(pattern)
    except (OSError, ValueError) as exc:
        raise KnowledgeStoreError(f"invalid migration knowledge file {path}: {exc}") from exc
    return matches
