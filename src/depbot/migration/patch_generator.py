"""Generate small, deterministic diffs for explicitly evidenced API renames."""

from __future__ import annotations

import difflib
import hashlib
import re
from collections import defaultdict
from pathlib import Path

from depbot.interfaces import StageError
from depbot.models import (
    BreakingChange,
    ChangeType,
    CodeUsage,
    ImpactLevel,
    PatchStatus,
    PlannedChange,
    SourcePatch,
)
from depbot.repository.paths import UnsafePath, safe_join


def _mapping(change: BreakingChange) -> tuple[str, str] | None:
    if (
        len(change.affected_symbols) != 1
        or len(change.replacement_symbols) != 1
        or change.change_type == ChangeType.UNKNOWN
        or not change.evidence_urls
    ):
        return None
    old, new = change.affected_symbols[0].strip(), change.replacement_symbols[0].strip()
    return (old, new) if old and new and old != new else None


def _diff(path: str, before: list[str], after: list[str]) -> str:
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )


def generate_patches(
    *,
    worktree: Path,
    plan: list[PlannedChange],
    changes: list[BreakingChange],
    usages: list[CodeUsage],
) -> list[SourcePatch]:
    """Create a patch only when the recorded source context still matches exactly."""
    by_id = {change.id: change for change in changes}
    usage_by_key: dict[tuple[str, str], list[CodeUsage]] = defaultdict(list)
    for usage in usages:
        for change_id in usage.related_change_ids:
            usage_by_key[(usage.file_path, change_id)].append(usage)

    patches: list[SourcePatch] = []
    for item in plan:
        if not item.related_change_ids:
            raise StageError(f"plan entry for {item.file_path} has no cited breaking change", blocked=True)
        try:
            path = safe_join(worktree, item.file_path)
        except UnsafePath as exc:
            raise StageError(f"unsafe planned path {item.file_path!r}: {exc}", blocked=True) from exc
        if not path.is_file():
            raise StageError(f"planned source file does not exist: {item.file_path}", blocked=True)

        # Read bytes first so universal-newline conversion does not turn CRLF
        # worktree content into an LF-only patch that git apply cannot match.
        before = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        after = list(before)
        touched: set[int] = set()
        touched_change_ids: set[str] = set()
        for change_id in item.related_change_ids:
            change = by_id.get(change_id)
            mapping = _mapping(change) if change else None
            if mapping is None:
                continue
            old, new = mapping
            identifier = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])")
            for usage in usage_by_key.get((item.file_path, change_id), []):
                if usage.impact != ImpactLevel.DEFINITE or usage.symbol != old:
                    continue
                if item.related_usage_lines and usage.line_start not in item.related_usage_lines:
                    continue
                if usage.line_start != usage.line_end:
                    continue  # multi-line evidence needs review; do not widen the edit.
                index = usage.line_start - 1
                if index < 0 or index >= len(after):
                    raise StageError(
                        f"stale usage line {item.file_path}:{usage.line_start}", blocked=True
                    )
                current = after[index]
                matches = list(identifier.finditer(current))
                if not matches:
                    # An already migrated line is not a reason to generate another patch.
                    if re.search(rf"(?<![A-Za-z0-9_]){re.escape(new)}(?![A-Za-z0-9_])", current):
                        continue
                    raise StageError(
                        f"recorded symbol {old!r} is absent from {item.file_path}:{usage.line_start}",
                        blocked=True,
                    )
                if len(matches) != 1:
                    raise StageError(
                        f"ambiguous occurrence count for {old!r} at {item.file_path}:{usage.line_start}",
                        blocked=True,
                    )
                if usage.snippet.strip() and usage.snippet.strip() not in current:
                    raise StageError(
                        f"recorded source snippet changed at {item.file_path}:{usage.line_start}",
                        blocked=True,
                    )
                after[index] = identifier.sub(new, current, count=1)
                touched.add(index)
                touched_change_ids.add(change_id)

        if not touched:
            continue
        patch_text = _diff(item.file_path, before, after)
        patch_id = "migration-" + hashlib.sha256(patch_text.encode("utf-8")).hexdigest()[:12]
        patches.append(
            SourcePatch(
                patch_id=patch_id,
                unified_diff=patch_text,
                files=[item.file_path],
                related_change_ids=sorted(touched_change_ids),
                status=PatchStatus.PROPOSED,
            )
        )
    return patches
