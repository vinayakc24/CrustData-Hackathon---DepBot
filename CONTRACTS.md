# DepBot Contract Handoff — v0.5.0

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

`detect_adapter` returns `ConanAdapter` (`conanfile.txt`) or `VcpkgAdapter` (`vcpkg.json`,
manifest mode). Both expose the same methods. C's verification must work for either:

```python
env = minimal_env(adapter.command_env())
install = adapter.install_args(worktree, build_type=bt)            # run first
preset = adapter.cmake_preset(build_type=bt)
if preset:   # Conan
    configure = ["cmake", "--preset", preset]
    build     = ["cmake", "--build", "--preset", preset]
    test      = ["ctest", "--preset", preset, "--output-on-failure"]
else:        # vcpkg
    configure = ["cmake", "-S", ".", "-B", "build", *adapter.cmake_configure_args(worktree, build_type=bt)]
    build     = ["cmake", "--build", "build", "--config", bt]
    test      = ["ctest", "--test-dir", "build", "-C", bt, "--output-on-failure"]
```

- `adapter.environment_problems()` is checked by the engine before any edit (missing
  `conan`, or missing `VCPKG_ROOT` / `vcpkg.cmake` / `vcpkg` executable → job BLOCKED).
- vcpkg installs into `<worktree>/vcpkg_installed/` (git-excluded, never counted as a change).
- vcpkg rules: only the target's `version>=` and/or `overrides` entry change (byte-minimal edit);
  `builtin-baseline` is never edited; a dependency with no `version>=` and no override is refused
  (reported as `unpinned(builtin-baseline)` → selection `needs_rescan` / BLOCKED). Whether the
  target resolves is proven only by the install step.

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

## 6. How the engine calls you (A's orchestrator, `depbot.orchestrator`)

For each eligible repository the engine:

1. Copies the repo to `artifacts/<run_id>/worktree/` (`.git`, `build/` and caches are
   skipped), runs `git init` there and commits a baseline on branch `depbot-baseline`,
   then checks out `depbot/<dep>-<version>`. A dirty source repo is refused. The
   original checkout is never written.
2. Detects the adapter, re-validates the declared version, then calls
   `verify(phase="baseline")`. A baseline that doesn't pass → job BLOCKED.
3. Calls `research`, then `impact`. Every usage must cite known change IDs and safe paths.
4. Calls `plan`, then applies the confidence policy gate.
5. Updates the manifest **itself** through the adapter, then calls `patch` and
   `apply` for each patch. Patches may touch only planned files (or the
   registry's `allowed_paths`). The manifest is off-limits to patches.
6. Calls `verify(phase="upgraded")`; the result maps to VERIFIED / NO_CHANGES_NEEDED / FAILED_*.
7. Always writes `state.json`, `patches/final.diff`, then calls `report` (best effort).

Notes for C:
- Baseline and upgraded verification run in the **same** worktree. Delete or
  re-create `build/` between phases (`build/` and `CMakeUserPresets.json` are git-excluded).
- **Run every install/configure/build/test command with
  `env=minimal_env(adapter.command_env())`** (`from depbot.package_managers.base import minimal_env`).
  This passes the isolated `CONAN_HOME` created by `depbot demo setup` and keeps tokens and API keys
  out of builds. Verified: with that env, `adapter.install_args()` resolves toydep offline.
- `depbot.repository.paths.safe_join(root, rel)` and `is_allowed(rel, allowed)` are
  available for the patch validator. Prefer them over re-implementing path checks.

### Repair stage (v0.2, optional) — C implements, A wires

`depbot.verification.repair_engine.propose_repair(*, worktree, state, failure, attempt) -> list[SourcePatch]`

- Called only after an upgraded FAILED_BUILD / FAILED_TESTS while `repair_attempts < max_repair_attempts`.
- `failure` is the failing `VerificationResult`; set its `failure_category` from your error classifier.
- Return targeted patches, or `[]` to give up. Do not run commands. The engine applies them through
  `validate_and_apply` (same scope rules), then re-runs the full upgraded verification.
- A patch whose diff was already applied is refused. Every repair patch is saved under `patches/`.
- If the module doesn't exist, repair is simply disabled; the job is not blocked.

### Delivery (A)

After VERIFIED or NO_CHANGES_NEEDED the engine writes `pr/pr-body.md` and `pr/pr.json` (local PR
artifact, built only from recorded evidence) when the pre-PR gate allows. With `create_pr=True`
it pushes the branch to the source's GitHub `origin` and runs `gh pr create` (no merge).
C's `write_repository_report` runs after this, so `state.pr_url` / `state.repo_facts["pull_request"]`
are available to the report.

A stage that isn't importable yet makes the job BLOCKED with
`stage '<name>' not integrated yet` — no fake success.

## 8. LLM runtime — `depbot.llm.runtime` (A owns; B and C consume)

Stage signatures stay frozen. A stage gets the current job's client from the runtime:

```python
from depbot.llm.base import UNTRUSTED_CONTENT_NOTICE, LLMError, wrap_untrusted
from depbot.llm.runtime import LLMNotConfigured, get_llm_client, current_llm_client

try:
    client = get_llm_client()                  # raises LLMNotConfigured if none is bound
except LLMNotConfigured:
    ...                                        # deterministic fallback, or StageError(..., blocked=True)

result = client.complete_structured(
    task="breaking_changes",                   # stable label; also the fixture key
    system=PROMPT + "\n" + UNTRUSTED_CONTENT_NOTICE,
    user=wrap_untrusted(changelog_excerpt),
    schema=MyPydanticModel,                    # response is validated, or LLMError is raised
)
```

- **Scoping:** the engine runs every repository job in a copied `contextvars` context and binds a
  fresh client from the run's factory, so clients never leak between jobs. `current_llm_client()`
  returns the client or `None`, for optional LLM use.
- **No client configured:** `get_llm_client()` raises `LLMNotConfigured` (a subclass of `LLMError`).
  If a stage lets any `LLMError` escape, the job fails with reason `LLM: …`, not as an internal error.
- **Tests:** `with use_llm_client(FixtureLLMClient({"breaking_changes": {...}})): ...`
- **Demo / CLI:** `--llm fixture --llm-fixtures path.json` (a `{task: response}` JSON file); each job
  gets its own `FixtureLLMClient`. The task labels and response shapes are B's (and C's) to define and
  document; the fixture file belongs next to the demo config.
- **Production:** `--llm anthropic [--llm-model M]` uses `depbot.llm.provider.AnthropicLLMClient`
  (stdlib HTTP, key from `LLM_API_KEY` or `ANTHROPIC_API_KEY`, model from `--llm-model` or
  `$DEPBOT_LLM_MODEL`, default `claude-sonnet-5-5`). The CLI fails fast if no key is set. No tools are
  offered to the model, and responses must be one JSON document matching the schema.
- **Default:** `--llm none` (or `$DEPBOT_LLM`). The deterministic demo must work in this mode or with fixtures.
- `depbot.llm.runtime` imports only `depbot.llm.base`.

## 9. Impact cascade (A: contracts + engine; B: layers; C: diagnostics)

Each layer settles every match it can prove, then hands only the residue to the next one.
The LLM goes last and never decides an edit on its own.

```text
I1 ripgrep prefilter → I2 libclang USR resolution → I3 compiler probe → I4 LLM (residue only)
```

**What A provides (implemented):**
- `CodeUsage` optional provenance: `resolved_by: ResolutionLayer` (`text_prefilter`, `clang_ast`,
  `compiler_probe`, `llm`, `human`), `resolution_proof`, `resolved_usr`, `declaration_file`,
  `preprocessor_context`. Validation rejects `resolved_by="llm"` with `impact="definite"`.
  A ruled-out text match should be kept as `impact="unaffected"` with `resolved_by="clang_ast"` and a proof.
- `AnalysisMethod.COMPILER_DIAGNOSTIC`, `CompilerDiagnostic`, `VerificationResult.diagnostics`.
- `adapter.command_env()` always sets `CMAKE_EXPORT_COMPILE_COMMANDS=ON`, so every configure writes
  `compile_commands.json`. After a passing baseline the engine copies it to
  `artifacts_dir/compile_commands.baseline.json` and records
  `state.repo_facts["compile_commands"] = {"baseline", "worktree", "external_include_dirs"}`.
  `external_include_dirs` are the include paths outside the repo (dependency headers, e.g. the Conan cache).
  Helpers: `depbot.repository.compile_db.load_compile_commands / include_dirs / external_include_dirs`.
- **Probe step** (`UpgradePolicy.run_probe_build=True`): after research and before impact, the engine copies
  the worktree to `artifacts_dir/probe/worktree`, bumps the dependency there, and calls
  `verify(phase=PROBE, artifacts_dir=artifacts_dir/"probe")`. It writes
  `artifacts_dir/probe/diagnostics.json` (a list of `CompilerDiagnostic`) and stores `state.probe`.
  The probe is advisory: it never changes the job status, and the real worktree is still on the old version
  when `impact` runs.
- Reference spike with results: `docs/spikes/clang_usr_resolution/` (11 text matches →
  6 confirmed, 4 ruled out with proof, 1 residual; no LLM).

**B — inside `find_impacted_usages` (signature unchanged):**
1. I1: ripgrep candidates (existing).
2. I2: `code_intelligence/clang_analyzer.py`. For each translation unit in
   `compile_commands.baseline.json` that contains a candidate, parse with its flags. A target
   declaration is one of the changed symbol whose file is under an `external_include_dirs` entry.
   Classify every candidate as confirmed (`definite`), ruled out (`unaffected`, with proof) or residual.
   Follow `docs/spikes/clang_usr_resolution/RESULTS.md` (using-declarations, macro bodies, token kinds).
3. I3: read `artifacts_dir/probe/diagnostics.json`. An error at a residual's file/line confirms it
   (`resolved_by="compiler_probe"`). An error that no breaking change explains becomes a new `CodeUsage`
   with `impact="unknown"` and a proof naming the diagnostic.
4. I4: only remaining residuals, and only if `current_llm_client()` is set. Use `task="impact_classification"`
   with the snippet ± context, `preprocessor_context`, the `BreakingChange`, and what I2/I3 proved.
   The answer may only be `possible` or `unaffected`, never `definite`.
5. Fallback without `compile_commands` or libclang: today's text-based behavior, with `resolved_by="text_prefilter"`.
   Dependency: `pip install "clang==18.1.8"` (bindings for the system libclang 18).

**C — verification:**
1. **Bug (blocks real Conan runs):** `pipeline._preset_build_dir` only reads `<worktree>/CMakePresets.json`.
   Conan 2 writes `CMakeUserPresets.json`, which `include`s `build/<Type>/generators/CMakePresets.json`.
   Follow the include chain. Otherwise the verifier configures without the Conan toolchain and
   `find_package` fails. The vendored fixtures hide this.
2. Fill `VerificationResult.diagnostics` from GCC/Clang output (`file:line:col: severity: message [-Wopt]`),
   with repository-relative paths (`None` for files outside the worktree), in every phase. The probe phase is the important one.
3. Phase `probe` is a normal verification run; no special casing is needed.

## 7. Change log

- 0.5.0 — additive: impact-cascade provenance on `CodeUsage` (`resolved_by`, `resolution_proof`, `resolved_usr`,
  `declaration_file`, `preprocessor_context`), `ResolutionLayer`, `AnalysisMethod.COMPILER_DIAGNOSTIC`,
  `CompilerDiagnostic` + `VerificationResult.diagnostics`, `VerificationPhase.PROBE`, `DepBotState.probe`,
  `UpgradePolicy.run_probe_build`; engine probe step and baseline compile-database capture;
  `command_env()` now always exports compile commands.
- 0.4.0 — additive: `depbot.llm.runtime` (`get_llm_client`, `current_llm_client`, `use_llm_client`,
  `LLMNotConfigured`, `fixture_factory`), production provider `depbot.llm.provider.AnthropicLLMClient`,
  CLI `--llm/--llm-fixtures/--llm-model`, and `run_repository_job(..., llm_factory=)` /
  `run_fleet(..., llm_factory=)`. Stage signatures unchanged.
- 0.3.0 — additive: `VcpkgAdapter` (manifest mode); `PackageManagerAdapter.cmake_configure_args()`
  and `.environment_problems()` (defaults `[]`, so Conan behaviour is unchanged). The engine now blocks
  a job before any edit if the package-manager tooling is unavailable.
- 0.2.0 — additive only, nothing existing changed:
  `PipelineStages.repair` (optional `RepairStage`); `RepositoryUpgradeJob.create_pr: bool = False`;
  `PackageManagerAdapter.command_env()` + `minimal_env()`; working copies of Git-root repos are now
  local clones sharing the source history (`repo_facts["working_copy"]["mode"] == "clone"`), with
  subdirectory fixtures still snapshot-copied.
- 0.1.1 — added `RESEARCHED → FAILED_MIGRATION`; documented engine call order, worktree layout,
  path helpers. `depbot run` / `depbot upgrade` available. No field changes.
- 0.1.0 — initial contracts (Member A).
