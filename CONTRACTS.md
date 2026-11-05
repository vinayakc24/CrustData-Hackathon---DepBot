# DepBot Contract Handoff — v0.1.0

Owner: **Member A**. These contracts are frozen for the first end-to-end demo.
B and C implement against them; if you need a change, propose it to A (field,
type, reason, example). Do not edit `models.py`, `state.py`, `interfaces.py`,
`package_managers/**`, `cli.py`, or `pyproject.toml`.

All models are Pydantic v2 with `extra="forbid"`: unknown fields fail validation.
Paths inside models are **repository-relative POSIX strings** unless noted.

```text
ReleaseEvent → BreakingChange → CodeUsage → PlannedChange → SourcePatch(validated)
  → CommandResult → VerificationResult → RepositoryUpgradeOutcome → FleetRunSummary
```

## 1. Models — `from depbot.models import ...`

| Model | Key fields (required **bold**) | Notes |
|---|---|---|
| `ReleaseEvent` | **event_id, dependency_name, upstream_repo, target_version**, previous_version, event_type=`release`, source_url, changelog_path (abs path after load), detected_at | `dedup_key` = (dep, from, to, type) |
| `Dependency` | **name, current_version, target_version, package_manager, manifest_path**, is_direct=True | |
| `RepositoryRecord` | **repository_id, repo_url_or_path, display_name**, policy_id, enabled=True, allowed_paths=[] | registry entry |
| `RepositoryDependencyRecord` | **repository_id, dependency_name, current_version, package_manager, manifest_path**, indexed_commit | inventory entry |
| `UpgradePolicy` | allowed_change_levels=[patch,minor], require_tests=True, require_baseline=True, minimum_migration_confidence=0.80, max_repair_attempts=2 (≤3), major_approved=False, auto_merge=False (True rejected) | |
| `BreakingChange` *(B produces)* | **id, title, change_type, affected_symbols, migration_advice, evidence_urls, confidence**, replacement_symbols=[], old_behavior, new_behavior, evidence_excerpt | non-`unknown` changes **must** have evidence_urls; confidence ∈ [0,1] |
| `CodeUsage` *(B produces)* | **symbol, file_path, line_start, line_end, snippet, analysis_method, impact, related_change_ids, confidence** | lines 1-based, end ≥ start; `analysis_method="ripgrep"` for text search |
| `PlannedChange` *(C produces)* | **file_path, reason, related_change_ids (≥1), expected_change**, related_usage_lines=[], risk=`low`, confidence=1.0 | every edit cites a change ID |
| `SourcePatch` *(C produces)* | **patch_id, unified_diff, files**, related_change_ids, status=`proposed`, rejection_reason, patch_path | status: proposed → validated → applied, or rejected |
| `ManifestChange` *(A's adapter produces)* | manifest_path, dependency_name, from_version, to_version, unified_diff | |
| `CommandResult` *(C produces)* | **command_label, args, cwd, exit_code, duration_seconds**, timed_out, stdout_path, stderr_path, stdout_tail, stderr_tail, output_truncated | `exit_code=None` if no exit code (timeout / not found); `.succeeded` |
| `VerificationResult` *(C produces)* | **phase** (`baseline`/`upgraded`), dependency_install, configure, build, tests, tests_passed, tests_failed, failed_test_names, passed, failure_category | `passed=True` is **rejected** unless install, configure and build all succeeded |
| `RepositoryUpgradeOutcome` *(A produces)* | **repository_id, status, classification**, reason, run_id, from/to_version, changed_files, report_path, pr_url, verification_passed | `status=verified` requires verification_passed=True |
| `FleetRunSummary` *(A produces)* | built only via `FleetRunSummary.from_outcomes(event, outcomes)` | counts derived, never hand-set |

### Enums (string values)

- `ChangeType`: removed, renamed, signature, behavior, deprecated, unknown
- `AnalysisMethod`: ripgrep, clang_ast, manual
- `ImpactLevel`: definite, possible, unaffected, unknown
- `RiskLevel`: low, medium, high
- `FleetClassification`: affected_candidate, unaffected, already_at_target_or_newer, disabled, needs_rescan
- `OutcomeStatus`: skipped, unaffected, verified, blocked, failed
- `PatchStatus`: proposed, validated, rejected, applied
- `VerificationPhase`: baseline, upgraded

## 2. Job state — `from depbot.state import DepBotState, JobStatus`

Only the orchestrator (A) calls `state.transition()`. Stages read state and return values.

```text
INITIALIZED → INSPECTED → RESEARCHED → IMPACT_ANALYZED → PLANNED → PATCHED
   → VERIFIED → PR_CREATED          (or NO_CHANGES_NEEDED after PATCHED)
Failures: BLOCKED (from any non-terminal), FAILED_RESEARCH, FAILED_MIGRATION,
          FAILED_INSTALL, FAILED_BUILD, FAILED_TESTS, FAILED_PR
Repair:   FAILED_BUILD | FAILED_TESTS → PLANNED | IMPACT_ANALYZED, at most max_repair_attempts
```

## 3. Stage functions you implement — `depbot.interfaces`

Implement a plain function with exactly this keyword-only signature at the listed
path. Raise `depbot.interfaces.StageError(msg, blocked=...)` for expected failures;
anything else is treated as a bug and fails the job.

| Owner | Import path | Signature → return |
|---|---|---|
| B | `depbot.research.breaking_change_analyzer.research_release` | `(*, event, dependency, artifacts_dir: Path) → list[BreakingChange]` |
| B | `depbot.code_intelligence.impact_analyzer.find_impacted_usages` | `(*, worktree: Path, changes, artifacts_dir: Path) → list[CodeUsage]` (empty list is valid) |
| C | `depbot.migration.planner.plan_migration` | `(*, dependency, changes, usages) → list[PlannedChange]` |
| C | `depbot.migration.patch_generator.generate_patches` | `(*, worktree, plan, changes, usages) → list[SourcePatch]` (source files only) |
| C | `depbot.migration.patch_validator.validate_and_apply` | `(*, worktree, patch, allowed_paths: list[str]) → SourcePatch` (APPLIED or REJECTED) |
| C | `depbot.verification.pipeline.run_verification` | `(*, worktree, adapter: PackageManagerAdapter, phase, policy, artifacts_dir) → VerificationResult` |
| C | `depbot.reporting.report_generator.write_repository_report` | `(*, state: DepBotState, artifacts_dir) → Path` |

Rules that apply to every stage:
- `worktree` is an isolated working copy that A creates. Never touch `RepositoryUpgradeJob.repo_root`.
- Every subprocess: argv list, `shell=False`, fixed `cwd`, timeout, bounded output.
- LLM use goes through `depbot.llm.base.LLMClient.complete_structured(task=..., schema=...)`. Wrap repository/changelog text with `wrap_untrusted()`. Use `FixtureLLMClient` in tests; the demo must work with no network.

## 4. Package-manager adapter — `depbot.package_managers` (A owns; C consumes)

```python
from depbot.package_managers.detector import detect_adapter
adapter = detect_adapter(worktree)          # ConanAdapter for conanfile.txt; raises UnsupportedManifest otherwise
adapter.find_dependency(worktree, "toydep") # DeclaredDependency(name, version, manifest_path, ...)
adapter.update_dependency(worktree, "toydep", "2.0.0")  # -> ManifestChange (A's orchestrator calls this)
adapter.install_args(worktree, build_type="Release")
#   -> ["conan", "install", ".", "--build=missing", "-s", "build_type=Release"]
adapter.cmake_preset(build_type="Release")  # -> "conan-release"
```

C's verification runs `install_args`, then `cmake --preset <preset>`,
`cmake --build --preset <preset>`, `ctest --preset <preset> --output-on-failure`.
Do not parse or edit manifests directly; ask A for a new adapter method.

## 5. Fixture agreement (C owns `tests/fixtures/**`)

These layouts were preflighted on Conan 2.32 / CMake 3.28 (v1 builds, unmigrated
v2 fails with `'old_sum' was not declared`, migrated v2 builds and passes CTest).

```text
tests/fixtures/toydep/1.0.0/{conanfile.py, CMakeLists.txt, include/toydep/toydep.h, src/toydep.cpp}
tests/fixtures/toydep/2.0.0/{... same ..., CHANGELOG.md}  # documents old_sum removed → sum
tests/fixtures/repos/repo_a/   conanfile.txt: toydep/1.0.0; src/main.cpp calls old_sum
tests/fixtures/repos/repo_b/   conanfile.txt: toydep/1.0.0; old_sum at a different call site (e.g. src/calc.cpp)
tests/fixtures/repos/repo_c/   conanfile.txt: toydep/2.0.0; already calls sum → skipped by selection
```

Each repo's `conanfile.txt` needs `[generators] CMakeDeps CMakeToolchain` and
`[layout] cmake_layout`; its `CMakeLists.txt` uses `find_package(toydep REQUIRED)`,
`target_link_libraries(... toydep::toydep)` and `enable_testing()` + `add_test(...)`.
The toydep packages are created locally with `conan create tests/fixtures/toydep/<v>`
(no network needed).

## 6. Change log

- 0.1.0 — initial contracts (Member A).
