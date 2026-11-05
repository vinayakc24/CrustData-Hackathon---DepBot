"""Evidence-constrained repair proposals for the orchestrator's bounded loop.

A repair is proposed only when the *upgraded* build's structured compiler
diagnostics (``VerificationResult.diagnostics``) report an error that

* names a symbol whose replacement is documented in the run's breaking-change
  evidence (one old symbol → one replacement, with evidence URLs), and
* points at a repository file and line where that symbol literally occurs.

Such a line is a usage the impact analysis missed (e.g. a file type the text
index does not cover). The repair becomes an ordinary plan + usage handed to the
migration patch generator, so it passes the same validation as any migration
patch. Everything else — configuration, linker, test failures, unexplained
symbols, macro token-pasting where the symbol is not on the reported line — is
declined, leaving the honest failure status. Repair never runs commands, never
edits tests, and never invents replacement APIs.
"""

from __future__ import annotations

import re
from pathlib import Path

from depbot.interfaces import StageError
from depbot.migration.patch_generator import _mapping, generate_patches
from depbot.models import (
    AnalysisMethod,
    CodeUsage,
    ImpactLevel,
    PlannedChange,
    ResolutionLayer,
    RiskLevel,
    SourcePatch,
    VerificationResult,
)
from depbot.repository.paths import UnsafePath, safe_join
from depbot.state import DepBotState
from depbot.verification.error_classifier import classify_failure


def _word(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")


def missed_usages(worktree: Path, state: DepBotState, failure: VerificationResult) -> list[CodeUsage]:
    """Compiler-proven usages of documented removed/renamed symbols, from diagnostics."""
    mappings = {}
    for change in state.breaking_changes:
        mapping = _mapping(change)
        if mapping is not None:
            mappings[mapping[0]] = change
    found: dict[tuple[str, int, str], CodeUsage] = {}
    for diag in failure.diagnostics:
        if diag.severity not in ("error", "fatal error") or not diag.file_path or not diag.line:
            continue
        for old, change in mappings.items():
            if not _word(old).search(diag.message):
                continue
            try:
                path = safe_join(worktree, diag.file_path)
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (UnsafePath, OSError):
                continue
            if diag.line > len(lines) or not _word(old).search(lines[diag.line - 1]):
                continue  # symbol not on the reported line (macro token-pasting etc.): decline
            key = (diag.file_path, diag.line, old)
            found.setdefault(key, CodeUsage(
                symbol=old, file_path=diag.file_path, line_start=diag.line, line_end=diag.line,
                snippet=lines[diag.line - 1].strip()[:1000], analysis_method=AnalysisMethod.COMPILER_DIAGNOSTIC,
                impact=ImpactLevel.DEFINITE, related_change_ids=[change.id], confidence=0.97,
                resolved_by=ResolutionLayer.COMPILER_PROBE,
                resolution_proof=f"upgraded build error: {diag.message}"[:500],
            ))
    return list(found.values())


def propose_repair(
    *,
    worktree: Path,
    state: DepBotState,
    failure: VerificationResult,
    attempt: int,
) -> list[SourcePatch]:
    if attempt < 1 or attempt > state.max_repair_attempts:
        return []
    if classify_failure(failure) != "removed_symbol":
        return []
    known = {patch.unified_diff for patch in state.patches}
    usages = missed_usages(worktree, state, failure)
    if not usages:
        # No compiler-located miss: only re-propose planned edits that were never applied
        # (e.g. diagnostics unavailable). Same evidence and validation; never a repeat diff.
        evidence = {s.lower() for c in state.breaking_changes for s in c.affected_symbols}
        output = "\n".join(t for r in (failure.configure, failure.build, failure.tests) if r is not None
                           for t in (r.stderr_tail, r.stdout_tail)).lower()
        if not any(s and s in output for s in evidence):
            return []
        try:
            planned = generate_patches(worktree=worktree, plan=state.migration_plan,
                                       changes=state.breaking_changes, usages=state.usages)
        except StageError:
            return []
        return [p for p in planned if p.unified_diff not in known]
    plan = []
    for file_path in sorted({u.file_path for u in usages}):
        mine = [u for u in usages if u.file_path == file_path]
        plan.append(PlannedChange(
            file_path=file_path,
            reason=f"repair attempt {attempt}: the upgraded build reports a documented removed symbol "
                   "the impact analysis did not find",
            related_change_ids=sorted({cid for u in mine for cid in u.related_change_ids}),
            related_usage_lines=sorted(u.line_start for u in mine),
            expected_change="; ".join(sorted({f"{u.symbol} -> documented replacement" for u in mine})),
            risk=RiskLevel.LOW,
            confidence=0.97,
        ))
    try:
        candidates = generate_patches(worktree=worktree, plan=plan, changes=state.breaking_changes, usages=usages)
    except StageError:
        return []  # stale or ambiguous evidence is a stop condition, not a repair
    return [p.model_copy(update={"patch_id": f"repair{attempt}-{p.patch_id}"})
            for p in candidates if p.unified_diff not in known]
