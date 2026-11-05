"""Render repository upgrade reports from structured evidence only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from depbot.reporting.evidence_collector import collect_evidence
from depbot.state import DepBotState


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _render_commands(commands: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Stage | Exit | Timed out | Duration (s) | Command | Logs |",
        "|---|---:|:---:|---:|---|---|",
    ]
    if not commands:
        return lines + ["| No command evidence | - | - | - | - | - |"]
    for result in commands:
        command = " ".join(result.get("args", []))
        logs = ", ".join(
            f"`{p}`" for p in (result.get("stdout_path"), result.get("stderr_path")) if p
        ) or "-"
        lines.append(
            f"| {_cell(result.get('command_label'))} | {_cell(result.get('exit_code'))} | "
            f"{_cell(result.get('timed_out'))} | {_cell(result.get('duration_seconds'))} | "
            f"`{_cell(command)}` | {logs} |"
        )
    return lines


def render_repository_report(evidence: dict[str, Any]) -> str:
    repo = evidence["repository"]
    dependency = evidence["dependency"]
    verification = evidence["verification"]
    changes = evidence["breaking_changes"]
    usages = evidence["usages"]
    plan = evidence["migration_plan"]
    patches = evidence["patches"]
    lines = [
        f"# DepBot repository upgrade report: {repo['repository_id']}",
        "",
        f"- Run: `{evidence['run']['run_id']}`",
        f"- Status: **{evidence['run']['status']}** — {_cell(evidence['run'].get('status_reason') or 'No status reason recorded')}",
        f"- Repository: `{repo['path']}`",
        f"- Dependency: `{dependency['name']}` {dependency['current_version']} → {dependency['target_version']}",
        f"- Generated: {evidence['generated_at']}",
        "",
        "## Research evidence",
        "",
    ]
    refs = evidence["research_references"]
    lines.extend([f"- {url}" for url in refs] or ["- No research references were recorded."])
    lines += ["", "## Breaking changes", ""]
    if changes:
        lines.extend([
            "| ID | Type | Affected symbols | Replacements | Confidence | Evidence |",
            "|---|---|---|---|---:|---|",
        ])
        for change in changes:
            evidence_refs = ", ".join(change.get("evidence_urls", [])) or "-"
            lines.append(
                f"| {_cell(change['id'])} | {_cell(change['change_type'])} | "
                f"{_cell(', '.join(change.get('affected_symbols', [])))} | "
                f"{_cell(', '.join(change.get('replacement_symbols', [])))} | "
                f"{_cell(change['confidence'])} | {_cell(evidence_refs)} |"
            )
    else:
        lines.append("No breaking changes were recorded.")
    lines += ["", "## Source usages and migration plan", ""]
    lines.append(f"Impacted symbols: {', '.join(evidence['impacted_symbols']) or 'none recorded'}.")
    if usages:
        lines.extend(["", "| Symbol | File | Lines | Impact | Change IDs | Confidence |", "|---|---|---:|---|---|---:|"])
        for usage in usages:
            lines.append(
                f"| `{_cell(usage['symbol'])}` | `{_cell(usage['file_path'])}` | "
                f"{usage['line_start']}–{usage['line_end']} | {_cell(usage['impact'])} | "
                f"{_cell(', '.join(usage['related_change_ids']))} | {_cell(usage['confidence'])} |"
            )
    else:
        lines.append("No source usages were recorded.")
    lines += ["", "### Planned edits", ""]
    if plan:
        for item in plan:
            lines.append(
                f"- `{item['file_path']}` lines {', '.join(map(str, item['related_usage_lines'])) or 'unspecified'}: "
                f"{_cell(item['expected_change'])} (risk: {item['risk']}, confidence: {item['confidence']:.2f})"
            )
    else:
        lines.append("No automatic source edits were planned.")
    lines += ["", "## Changed files and patch validation", ""]
    lines.extend([f"- `{path}`" for path in evidence["changed_files"]] or ["- No changed files were recorded."])
    if patches:
        for patch in patches:
            lines.append(
                f"- Patch `{patch['patch_id']}`: **{patch['status']}** — "
                f"{_cell(patch.get('rejection_reason') or ', '.join(patch.get('files', [])))}"
            )
    else:
        lines.append("- No source patches were recorded.")
    manifest = evidence.get("manifest_change")
    if manifest:
        lines.append(
            f"- Dependency manifest: `{manifest['manifest_path']}` {manifest['from_version']} → {manifest['to_version']}"
        )
    upgraded = verification["upgraded"]
    if upgraded is None:
        ctest_result = "not run"
        test_counts = "not recorded"
    elif upgraded.get("tests") is None:
        ctest_result = "not run"
        test_counts = "not recorded"
    else:
        ctest_result = "passed" if upgraded["tests"].get("exit_code") == 0 and not upgraded["tests"].get("timed_out") else "failed"
        test_counts = (
            f"{upgraded.get('tests_passed')} passed, {upgraded.get('tests_failed')} failed"
            if upgraded.get("tests_passed") is not None
            else "counts unavailable in command output"
        )
    lines += ["", "## Verification", "", f"- Baseline passed: **{verification['baseline'].get('passed') if verification['baseline'] else 'not run'}**",
              f"- Upgraded verification passed: **{upgraded.get('passed') if upgraded else 'not run'}**",
              f"- CTest: **{ctest_result}**; {test_counts}",
              "", *_render_commands(verification["commands"]), ""]
    for phase_name in ("baseline", "upgraded"):
        phase = verification.get(phase_name)
        if phase and phase.get("failure_category"):
            lines.append(f"- {phase_name.title()} failure category: `{phase['failure_category']}`")
    lines += ["", "## Repair and remaining risks", "", f"- Repair attempts recorded: {evidence['repair_attempts']}"]
    risks = evidence["remaining_risks"]
    lines.extend([f"- {risk}" for risk in risks] or ["- No additional risks were recorded."])
    lines += ["", "Verification status reflects recorded subprocess results; no result is inferred when a command or test was not run.", ""]
    return "\n".join(lines)


def write_repository_report(*, state: DepBotState, artifacts_dir: Path) -> Path:
    """Write JSON evidence and Markdown report beneath this run's artifact directory."""
    evidence = collect_evidence(state)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifacts_dir / "evidence.json"
    markdown_path = artifacts_dir / "repository-report.md"
    json_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_repository_report(evidence), encoding="utf-8")
    return markdown_path
