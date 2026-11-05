"""Fleet-level summary rendering (Member A). Per-repository reports are Member C's."""

from __future__ import annotations

from depbot.models import FleetRunSummary, OutcomeStatus

_ICON = {
    OutcomeStatus.VERIFIED: "VERIFIED",
    OutcomeStatus.UNAFFECTED: "UNAFFECTED",
    OutcomeStatus.SKIPPED: "SKIPPED",
    OutcomeStatus.BLOCKED: "BLOCKED",
    OutcomeStatus.FAILED: "FAILED",
}


def _cell(text: str | None) -> str:
    return (text or "-").replace("|", "\\|").replace("\n", " ")


def render_fleet_markdown(summary: FleetRunSummary, fleet_id: str) -> str:
    lines = [
        f"# DepBot fleet run: {summary.event_id}",
        "",
        f"- Fleet: `{fleet_id}`",
        f"- Dependency: `{summary.dependency_name}` {summary.from_version or '?'} → {summary.to_version}",
        f"- Generated: {summary.generated_at}",
        "",
        "| Considered | Affected | Verified | Skipped | Failed/blocked |",
        "|---:|---:|---:|---:|---:|",
        f"| {summary.repositories_considered} | {summary.repositories_affected} | "
        f"{summary.repositories_verified} | {summary.repositories_skipped} | "
        f"{summary.repositories_failed_or_blocked} |",
        "",
        "| Repository | Outcome | Selection | Versions | Changed files | Reason | Report |",
        "|---|---|---|---|---|---|---|",
    ]
    for o in summary.outcomes:
        versions = f"{o.from_version or '?'} → {o.to_version or '?'}"
        changed = ", ".join(f"`{f}`" for f in o.changed_files) or "-"
        lines.append(
            f"| {o.repository_id} | {_ICON[o.status]} | {o.classification.value} | {versions} | "
            f"{changed} | {_cell(o.reason)} | {_cell(o.report_path)} |"
        )
    lines += [
        "",
        "Statuses reflect observed execution only: VERIFIED means the upgraded "
        "install, configure, build and tests actually ran and passed in an isolated working copy.",
        "",
    ]
    return "\n".join(lines)
