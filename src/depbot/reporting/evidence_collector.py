"""Collect per-repository report data from typed run state and command evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from depbot.state import DepBotState


def _verification(value) -> dict[str, Any] | None:
    return value.model_dump(mode="json") if value is not None else None


def collect_evidence(state: DepBotState) -> dict[str, Any]:
    """Build a serializable evidence record; all status fields come from run state."""
    possible_usages = [
        usage.model_dump(mode="json")
        for usage in state.usages
        if usage.impact.value in {"possible", "unknown"}
    ]
    unknown_changes = [
        change.id for change in state.breaking_changes if change.change_type.value == "unknown"
    ]
    rejected_patches = [
        patch.model_dump(mode="json") for patch in state.patches if patch.status.value == "rejected"
    ]
    risks: list[str] = []
    if possible_usages:
        risks.append(f"{len(possible_usages)} possible/unknown usage(s) were not automatically edited")
    if unknown_changes:
        risks.append("breaking changes marked unknown: " + ", ".join(unknown_changes))
    if rejected_patches:
        risks.append(f"{len(rejected_patches)} patch(es) were rejected")
    if state.baseline is not None and not state.baseline.passed:
        risks.append("baseline verification failed; upgraded results cannot be separated from pre-existing failures")
    if state.verification is not None and not state.verification.passed:
        risks.append(
            f"upgraded verification did not pass ({state.verification.failure_category or 'unclassified failure'})"
        )
    if state.status_reason:
        risks.append(state.status_reason)

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository": {
            "repository_id": state.repository_id,
            "path": state.original_repo,
            "upstream_repo": state.upstream_repo,
            "working_branch": state.working_branch,
            "base_branch": state.base_branch,
        },
        "run": {
            "run_id": state.run_id,
            "status": state.status.value,
            "status_reason": state.status_reason,
            "history": [entry.model_dump(mode="json") for entry in state.history],
        },
        "dependency": state.dependency.model_dump(mode="json"),
        "research_references": sorted(
            {url for change in state.breaking_changes for url in change.evidence_urls}
        ),
        "breaking_changes": [change.model_dump(mode="json") for change in state.breaking_changes],
        "impacted_symbols": sorted({usage.symbol for usage in state.usages}),
        "usages": [usage.model_dump(mode="json") for usage in state.usages],
        "review_required_usages": possible_usages,
        "migration_plan": [item.model_dump(mode="json") for item in state.migration_plan],
        "patches": [patch.model_dump(mode="json") for patch in state.patches],
        "manifest_change": (
            state.manifest_change.model_dump(mode="json") if state.manifest_change is not None else None
        ),
        "changed_files": state.changed_files,
        "verification": {
            "baseline": _verification(state.baseline),
            "upgraded": _verification(state.verification),
            "commands": [item.model_dump(mode="json") for item in state.command_results],
        },
        "repair_attempts": state.repair_attempts,
        "remaining_risks": risks,
        "working_copy": state.repo_facts.get("working_copy"),
        "additional_facts": state.repo_facts,
    }
