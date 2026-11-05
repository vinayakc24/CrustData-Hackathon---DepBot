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
    if upgraded is None or upgraded.get("tests") is None:
        ctest_result = "not run"
        test_counts = "not recorded"
    else:
        tests = upgraded["tests"]
        passed_count, failed_count = upgraded.get("tests_passed"), upgraded.get("tests_failed")
        if tests.get("timed_out"):
            ctest_result = "timed out"
        elif tests.get("exit_code") is None:
            ctest_result = "no exit code recorded"
        elif passed_count is not None and failed_count is not None:
            ctest_result = (
                "passed" if tests.get("exit_code") == 0 and passed_count > 0 and failed_count == 0
                else "failed" if failed_count > 0 or tests.get("exit_code") != 0
                else "no tests recorded"
            )
        else:
            ctest_result = f"exit code {tests.get('exit_code')} (test counts not recorded)"
        test_counts = (
            f"{passed_count} passed, {failed_count} failed"
            if passed_count is not None and failed_count is not None
            else "not recorded"
        )
    lines += [
        "",
        "## Verification",
        "",
        f"- Baseline verification passed: **{verification['baseline'].get('passed') if verification['baseline'] else 'not run'}**",
        f"- Upgraded verification passed: **{upgraded.get('passed') if upgraded and upgraded.get('passed') is not None else 'not recorded'}**",
        f"- CTest: **{ctest_result}**; {test_counts}",
        "",
        "| Phase | Verification passed | CTest command | Test counts | Failed test names |",
        "|---|:---:|---|---|---|",
    ]
    for phase_name in ("baseline", "upgraded"):
        phase = verification.get(phase_name)
        if phase is None:
            lines.append(f"| {phase_name.title()} | not run | not run | not recorded | not recorded |")
            continue
        test_command = phase.get("tests")
        if test_command is None:
            ctest_status, counts, failed_names = "not run", "not recorded", "not recorded"
        else:
            if test_command.get("timed_out"):
                ctest_status = "timed out"
            elif test_command.get("exit_code") is None:
                ctest_status = "no exit code recorded"
            else:
                ctest_status = f"exit {test_command.get('exit_code')}"
            passed_count, failed_count = phase.get("tests_passed"), phase.get("tests_failed")
            counts = (
                f"{passed_count} passed, {failed_count} failed"
                if passed_count is not None and failed_count is not None
                else "not recorded"
            )
            names = phase.get("failed_test_names")
            if names:
                failed_names = ", ".join(names)
            elif failed_count == 0:
                failed_names = "none"
            else:
                failed_names = "not recorded"
        phase_passed = phase.get("passed")
        phase_passed = str(phase_passed) if phase_passed is not None else "not recorded"
        lines.append(
            f"| {phase_name.title()} | {phase_passed} | {_cell(ctest_status)} | "
            f"{_cell(counts)} | {_cell(failed_names)} |"
        )
    lines.extend(["", *_render_commands(verification["commands"]), ""])
    for phase_name in ("baseline", "upgraded"):
        phase = verification.get(phase_name)
        if phase and phase.get("failure_category"):
            lines.append(f"- {phase_name.title()} failure category: `{phase['failure_category']}`")

    pull_request = evidence.get("pull_request")
    lines += ["", "## Pull-request artifact", ""]
    if not isinstance(pull_request, dict):
        lines.append("No pull-request artifact status was recorded.")
    else:
        published = pull_request.get("published")
        if published is None and pull_request.get("skipped"):
            published = False
        published_text = str(published) if published is not None else "not recorded"
        lines.append(f"- Published to GitHub: **{_cell(published_text)}**")
        url = pull_request.get("url")
        if url:
            lines.append(f"- URL: {url}")
        note = pull_request.get("publish_note") or pull_request.get("skipped")
        if note:
            lines.append(f"- Note: {_cell(note)}")
        elif published is None:
            lines.append("- Note: not recorded")

    decisions = evidence.get("policy_decisions")
    lines += ["", "## Policy decisions", ""]
    if decisions:
        for index, decision in enumerate(decisions, start=1):
            lines.extend([f"### Decision {index}", "", "```json", json.dumps(decision, indent=2, ensure_ascii=False), "```", ""])
    else:
        lines.append("No policy decisions were recorded.")

    lines += ["", "## Repair and remaining risks", "", f"- Repair transitions recorded: {evidence['repair_attempts']}"]
    attempts = evidence.get("repair_attempt_details")
    if attempts:
        for attempt in attempts:
            lines.append(f"- Repair attempt {attempt.get('attempt', 'not recorded')}:")
            patches = attempt.get("patches")
            if not patches:
                lines.append("  - No patch entries were recorded for this attempt.")
                continue
            for patch in patches:
                paths = patch.get("paths") or []
                path_text = ", ".join(f"`{path}`" for path in paths) or "no path recorded"
                status = patch.get("status") or "not recorded"
                artifact_path = patch.get("artifact_path")
                artifact_text = f"; artifact `{artifact_path}`" if artifact_path else "; artifact path not recorded"
                lines.append(
                    f"  - Patch `{_cell(patch.get('patch_id') or 'not recorded')}`: "
                    f"{path_text}{artifact_text}; status **{_cell(status)}**"
                )
    else:
        lines.append("No per-attempt repair details were recorded.")
    risks = evidence["remaining_risks"]
    lines.extend([f"- {risk}" for risk in risks] or ["- No additional risks were recorded."])
    lines += ["", "Verification status reflects recorded subprocess results; no result is inferred when a command or test was not run.", ""]
    return "\n".join(lines)


def write_repository_report(*, state: DepBotState, artifacts_dir: Path) -> Path:
    """Write JSON evidence and Markdown report beneath this run's artifact directory."""
    evidence = collect_evidence(state)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifacts_dir / "evidence.json"
    markdown_path = artifacts_dir / "upgrade-report.md"
    rendered = render_repository_report(evidence)
    json_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(rendered, encoding="utf-8")
    # Keep the existing filename as a compatibility copy for downstream consumers.
    (artifacts_dir / "repository-report.md").write_text(rendered, encoding="utf-8")
    return markdown_path
