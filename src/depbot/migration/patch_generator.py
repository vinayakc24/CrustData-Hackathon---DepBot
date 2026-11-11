"""Generate small, deterministic diffs by running registered migration recipes."""

from __future__ import annotations

import difflib
import hashlib
from collections import defaultdict
from pathlib import Path

from depbot.interfaces import StageError
from depbot.migration.recipes import DELETED, select_recipe
from depbot.migration.recipes import unique_mapping as _mapping  # noqa: F401  (used by the repair engine)
from depbot.models import (
    BreakingChange,
    CodeUsage,
    PatchStatus,
    PlannedChange,
    SourcePatch,
)
from depbot.repository.paths import UnsafePath, safe_join


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
            recipe = select_recipe(change) if change else None
            if change is None or recipe is None:
                continue  # no registered recipe covers this change: nothing is generated
            cited = [
                usage for usage in usage_by_key.get((item.file_path, change_id), [])
                if recipe.matches(change, usage)
                and (not item.related_usage_lines or usage.line_start in item.related_usage_lines)
            ]
            current = list(after)
            result = recipe.transform(lines=current, change=change, usages=cited, file_path=item.file_path)
            if len(result.lines) != len(current):
                raise StageError(f"recipe {recipe.metadata.recipe_id} changed the line count", blocked=True)
            drift = [i for i in range(len(current)) if result.lines[i] != current[i] and i not in result.touched]
            violations = recipe.postconditions(
                before=current, after=result.lines, touched=result.touched, change=change
            )
            if drift:
                violations.append(f"lines {[i + 1 for i in drift]} changed without being reported")
            if violations:
                raise StageError(
                    f"recipe {recipe.metadata.recipe_id} postcondition failed for {item.file_path}: "
                    + "; ".join(violations),
                    blocked=True,
                )
            after = result.lines
            if result.touched:
                touched |= result.touched
                touched_change_ids.add(change_id)

        if not touched:
            continue
        patch_text = _diff(item.file_path, before, [line for line in after if line != DELETED])
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
