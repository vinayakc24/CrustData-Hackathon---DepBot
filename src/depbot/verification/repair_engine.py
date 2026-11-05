"""Evidence-constrained repair proposals for the orchestrator's bounded loop."""

from __future__ import annotations

from pathlib import Path

from depbot.models import SourcePatch, VerificationResult
from depbot.migration.patch_generator import generate_patches
from depbot.state import DepBotState
from depbot.verification.error_classifier import classify_failure


def propose_repair(
    *,
    worktree: Path,
    state: DepBotState,
    failure: VerificationResult,
    attempt: int,
) -> list[SourcePatch]:
    """Repair a removed-symbol failure only when original evidence covers it.

    This stage deliberately declines configuration, linker, test, and unrelated
    compile failures. It never edits tests or invents replacement APIs.
    """
    if attempt < 1 or attempt > state.max_repair_attempts:
        return []
    if classify_failure(failure) != "removed_symbol":
        return []
    output = "\n".join(
        text
        for result in (failure.configure, failure.build, failure.tests)
        if result is not None
        for text in (result.stderr_tail, result.stdout_tail)
    ).lower()
    evidence_symbols = {
        symbol.lower()
        for change in state.breaking_changes
        for symbol in change.affected_symbols
    }
    if not any(symbol and symbol in output for symbol in evidence_symbols):
        return []
    try:
        candidates = generate_patches(
            worktree=worktree,
            plan=state.migration_plan,
            changes=state.breaking_changes,
            usages=state.usages,
        )
    except Exception:
        # A stale or ambiguous evidence record is a stop condition, not a repair.
        return []
    known_diffs = {patch.unified_diff for patch in state.patches}
    return [patch for patch in candidates if patch.unified_diff not in known_diffs]
