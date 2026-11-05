# DepBot — Hackathon Implementation Plan

> **Project:** DepBot — Autonomous C++ Dependency Upgrade Engineer  
> **Format:** 12-hour hackathon  
> **Team:** 3 people, using Claude Code as a development accelerator  
> **Primary outcome:** Demonstrate an autonomous dependency-maintenance workflow across a small fleet of C++ repositories: detect a release event, identify affected repositories, migrate selectively, verify each result, and produce evidence-backed pull requests.
>
> **Product direction:** DepBot is designed as an always-on, multi-repository dependency maintenance platform. The 12-hour build implements a credible vertical slice of that architecture—not merely a single-repository upgrade command.

---

## 1. Product goal

DepBot is an always-on autonomous dependency maintenance engineer for fleets of software repositories. It monitors upstream dependency releases and security/deprecation signals, maintains an organization-wide view of which repositories use which dependency versions and APIs, identifies which repositories are affected by a change, and selectively runs migrations. Each migration is isolated, verified, policy-checked, and delivered as an evidence-backed pull request—without requiring a developer to notice the release or prompt a coding agent.

A developer can still trigger a targeted upgrade manually, but that is one entry point into the same engine, not the product's defining workflow.

The product is **not** a version-bump script, a thin wrapper around Claude Code, or an LLM that blindly edits source files. It is a persistent engineering system: deterministic orchestration and repository/dependency state around bounded LLM reasoning, with repeatable verification and accumulated migration knowledge.

### Fleet-level workflow

1. **Monitor** — detect upstream releases, security advisories, and relevant deprecation signals on a schedule or via webhooks.
2. **Resolve event** — normalize the release into a versioned `ReleaseEvent`, identify the upstream project and affected version range, and deduplicate the event.
3. **Select repositories** — query the organization-wide dependency/API index to find candidate repositories; skip repositories that are unaffected or already upgraded.
4. **Research and analyze** — inspect release notes, changelogs, migration guides, and (where feasible) public-header changes; map breaking changes to actual usages in each candidate repository.
5. **Migrate** — create a repository-specific plan, update the dependency declaration, and generate validated source patches in an isolated worktree.
6. **Verify and repair** — install, configure, build, test, diagnose failures, and perform bounded repairs under repository/organization policy.
7. **Deliver and learn** — open evidence-backed PRs, update fleet status, and store validated migration outcomes/patterns for future runs.

A manual `upgrade` command invokes the same pipeline for one repository. The autonomous monitor is the normal long-term entry point.

### Definition of success

A run is a **verified success** only when all of the following are true:

- The target dependency and version are unambiguously identified.
- The target version resolves through the selected package manager.
- The migration changes are applied in an isolated working copy.
- The build succeeds using the repository's supported build configuration.
- All tests that passed in the baseline still pass after the upgrade. Any pre-existing failures are explicitly disclosed.
- The final diff is reviewed for unintended changes.
- The PR report contains source references, files changed, actual command results, test counts, and known limitations.
- A PR is created only after the configured verification gate passes. A failed run may create a local diagnostic report, but must not be labelled successful.

A PR can still be created in an explicitly requested **draft/needs-attention** mode after a failed verification, but it must be clearly marked as unverified and must never claim the upgrade works. This mode is optional and should not delay the main demo.

---

## 2. Scope and priorities

Twelve hours is enough to build a meaningful vertical slice, but not enough to make every C++ repository and package manager work. Keep the product focused on CMake projects and one direct dependency upgrade per run.

### Product scope versus hackathon scope

The standout product is a **fleet maintenance platform**, not a one-repository command. The hackathon must make that visible in its architecture and demo while keeping implementation bounded.

**Hackathon vertical slice:** register three local/GitHub C++ repositories; trigger a simulated or manually polled upstream release event; inspect their dependency manifests and indexed usages; identify two affected repositories and one unaffected/already-upgraded repository; run isolated migration jobs for only the affected repositories (parallel execution if stable); verify each independently; and produce per-repository reports plus one fleet summary. The release event can be fixture-backed; the migration/build/test path must be real. A live scheduled monitor is a stretch if the core path is stable.

**Long-term platform architecture (design now, implement incrementally):**

- **Release Monitor:** scheduled polling and/or webhooks for upstream releases, security advisories, and deprecations; event normalization, deduplication, and version-range tracking.
- **Organization/Repository Registry:** registered repositories, ownership, build environments, package-manager configuration, policy assignment, and scan status.
- **Dependency & API Index:** a queryable mapping from repository → dependency/version → headers/symbols/usages, updated incrementally when a repository or dependency changes. Use it to select only potentially affected repositories rather than launching an LLM workflow for every repo.
- **Fleet Orchestrator:** risk-aware queue, bounded parallel workers, concurrency/resource limits, retry handling, cancellation, per-repository isolation, and fleet-level progress/status.
- **Migration Knowledge Base:** store source-backed breaking-change mappings, reviewed migration patterns, successful patch examples, failure/repair outcomes, and verification evidence. Retrieve similar patterns as context; never blindly replay patches across repositories.
- **Policy Engine:** repository/team-level rules for allowed version ranges, required builds/tests/sanitizers, confidence thresholds, maximum repair attempts, PR/draft behavior, approval requirements, and prohibition on automatic merging by default.
- **Evidence & Analytics:** auditable release-to-PR lineage, success rate, affected-repository precision, repair/cost/runtime metrics, and recurring migration failure patterns.

These are architectural commitments and post-hackathon roadmap items; they are not a promise to fully implement a hosted service in 12 hours.

### P0 — must work for the demo

| Area | P0 capability | Acceptance condition |
|---|---|---|
| Fleet registry | Register and inspect three repositories from a small config/SQLite registry | One release event is evaluated against all registered repositories |
| Dependency inventory | Discover direct dependency name/version/manager per repo; persist scan results | Affected, unaffected, and already-upgraded repos are distinguished |
| Release event | Fixture-backed event input or explicit release command; normalize dependency + from/to versions | Event is deduplicated and triggers fleet impact analysis without a per-repo prompt |
| Repository execution | Local path or GitHub URL; isolated clone/worktree; dirty-tree check | Original working tree is never modified; each repo has isolated state |
| Selective orchestration | Queue only affected repositories; bounded parallelism (or sequential fallback) | Unaffected repositories do not invoke migration/LLM/build workers |
| Build system | CMake | Configure/build/test commands can be detected or supplied |
| Package managers | Conan 2 `conanfile.txt`; vcpkg manifest mode where the target version is explicitly resolvable | Correct direct dependency is updated, or a clear safe refusal is returned |
| Research | GitHub releases/changelog and upstream source discovery | Every important breaking-change claim has a source URL or is marked unverified |
| Code impact | `ripgrep`-based usage discovery with file/line/snippet; contextual LLM classification | Relevant matches are included and false positives are labelled uncertain |
| Migration | Structured plan + unified diff + patch validation | Only approved files/changes are applied |
| Verification | Baseline and upgraded install/configure/build/CTest | Real exit codes and logs are collected |
| Repair | Up to 2–3 bounded repair attempts | Every attempt is diffed and re-tested; no infinite loop |
| Delivery | Report + GitHub PR through `gh` or API | PR body accurately reflects verification |
| Tests | Unit tests plus deterministic three-repository fleet fixture | Release event selects the right repos; migrations and verification run without live GitHub/LLM calls |
| Policy gate | Minimal YAML policy for required tests, max repair attempts, major/low-confidence approval, and no auto-merge | Job/PR decision follows a recorded policy outcome |
| Fleet report | Per-repo results plus aggregate event summary | Report explains migrated, skipped, blocked, and failed repositories |

### P1 — implement if P0 is stable

- Conan `conanfile.py` support for simple, statically recognizable `requires` declarations. Do not execute or rewrite arbitrary Python recipe logic automatically.
- vcpkg baseline updates, only when the version resolver can determine the exact impact and the user has permitted baseline movement. Otherwise stop and explain.
- CMake Debug and Release builds.
- `compile_commands.json` generation and optional Clang-based semantic analysis.
- Public-header diff between old and new upstream tags.
- `clang-tidy` or sanitizer pass, when available and time permits.
- A compact HTML or Markdown run summary.
- A real scheduled release poller (fixture/manual event is sufficient for P0); webhook ingestion.
- Persistent SQLite registry and incremental dependency/API index beyond the demo fixture.
- Bounded concurrent migration workers if sequential orchestration is the reliable P0 path.

### P2 — stretch / post-hackathon

- Bazel, Meson, Make, other package managers.
- Multiple dependency upgrades in one run.
- Automatic test generation or test rewriting.
- ABI compatibility analysis as a required gate.
- Full semantic indexing across templates/macros and generated code.
- Automatic PR merging.
- Hosted multi-tenant service and web dashboard.
- Large-scale distributed worker fleet, advanced queueing, and organization-wide analytics.
- Fully automated security/deprecation policy remediation across arbitrary ecosystems.

**Scope rule:** Do not start a P1 feature while the P0 vertical slice is broken. At hour 7, freeze new features and integrate.

---

## 3. Key design decisions

### 3.1 CMake is the supported build system

DepBot detects `CMakeLists.txt` and optionally `CMakePresets.json`. It should support a repository-provided configure/build/test command override. Do not attempt to infer every compiler/build tool combination.

### 3.2 Package-manager support is adapter-based

Create a common interface so the orchestrator does not contain Conan- or vcpkg-specific logic.

- **Conan 2:** P0 support for `conanfile.txt`. Parse `[requires]`, update only the selected direct requirement, install the resolved graph, and retain relevant lockfile changes. `conanfile.py` is P1 and must be treated cautiously because it is executable Python.
- **vcpkg:** P0 only for manifest-mode projects where the version constraint and configured registry/baseline make the target version resolvable without unexpectedly upgrading unrelated dependencies. Understand `vcpkg.json`, `vcpkg-configuration.json`, and the project's lockfile if present. If the target requires a baseline/registry change that cannot be safely isolated, refuse automatic modification and explain the required action. Never blindly rewrite the baseline.
- The adapter must report whether a dependency is direct or transitive. Each repository job upgrades one direct dependency. Transitive changes are recorded as resolution impact, not independently upgraded by default.

### 3.3 Claude Code is a development tool, not DepBot's runtime

Claude Code helps the team implement DepBot. DepBot itself calls a configured LLM API through a small provider abstraction. Do not depend on an interactive Claude Code session or Claude Code CLI for production runtime behavior.

Implement one runtime provider for the hackathon. Keep the provider behind an `LLMClient` interface so another provider can be added later.

### 3.4 Evidence before edits

The research stage emits sourced `BreakingChange` records. The impact stage links these to concrete application usages. The migration planner must cite those records. If evidence is missing, the agent can report uncertainty, but must not invent an API replacement.

### 3.5 Hybrid code analysis

- P0: `ripgrep`/structural text search, extension filters, line numbers, include-path context, and LLM classification of snippets.
- P1: generate `compile_commands.json` and run an optional Clang-based helper (`libclang`, LibTooling, or another available tool) to resolve declarations/references. Do not make AST availability a blocker for a text-based migration.
- Record analysis method and confidence per usage. Do not label textual matches as semantically confirmed.

### 3.6 Isolated execution and least privilege

Repository build scripts and tests execute arbitrary code. Run them in a disposable container or isolated environment where feasible; do not mount GitHub credentials into the build container. Use a clean worktree/clone, command timeouts, output limits, and a fixed working directory. Never let the LLM provide arbitrary shell commands that are executed without policy validation. Treat repository content, release notes, and issue text as untrusted data, not instructions to the agent.

### 3.7 Fleet-first product architecture; bounded hackathon implementation

Separate the system into three cooperating layers:

1. **Fleet Intelligence:** release monitor, repository registry, dependency/API index, migration knowledge base, and policy engine.
2. **Migration Engine:** the existing inspect → research → impact → plan → patch → verify → report pipeline, executed independently for one repository/dependency event.
3. **Fleet Orchestrator:** event deduplication, affected-repository selection, bounded scheduling/concurrency, isolation, and aggregate reporting.

The migration engine must not own fleet-wide scheduling or global registry state. A repository migration receives a well-defined job payload and produces a structured result. This keeps per-repository runs reproducible and allows the fleet orchestrator to retry, schedule, or scale them independently.

For the hackathon, implement a minimal registry/event/index abstraction and three-repository demonstration. A local SQLite database or versioned JSON store is sufficient; do not introduce a distributed database, message broker, Kubernetes, or microservices. Keep interfaces ready for those future implementations without building them now.

### 3.8 Incremental indexing and shared migration knowledge

Store dependency inventory and discovered API usages with repository revision/commit metadata. Re-index only when the repository revision, relevant manifests, or indexed source files change. On a release event, first use dependency/version and symbol indexes to narrow the candidate set; then validate current checkout state before migration.

Persist migration patterns only with provenance: upstream evidence, old/new symbols, applicable version range, context, patch summary, confidence, and verification outcome. Retrieve these patterns to inform planning in similar repositories, but require fresh contextual analysis, patch validation, and tests for every repository.

### 3.9 Policy-driven autonomy

Use a policy decision before migration and before PR creation. Example risk tiers: compatible patch releases may be auto-migrated and submitted as PRs; minor releases with API changes require stronger verification; major or low-confidence migrations may produce draft PRs or request human approval. DepBot must never silently merge by default. Policies should be explicit, inspectable, and included in the evidence report.

---

## 4. Architecture

DepBot has a fleet layer around a repository-scoped migration engine. Use deterministic Python orchestration and specialized modules; avoid independent conversational agents. LLMs perform bounded reasoning tasks, while Python owns registry state, event scheduling, file access, Git, package-manager commands, builds, tests, and policy enforcement.

```text
                    UPSTREAM ECOSYSTEMS
           Releases / Advisories / Deprecations
                           |
                           v
                 +--------------------+
                 | Release Monitor    |
                 | Poller / Webhooks  |
                 | Event Normalizer   |
                 +--------------------+
                           |
                           v
+-------------------------- FLEET CONTROL PLANE --------------------------+
|  Repository Registry       Dependency + API Index                      |
|  (repos, owners, policies)  (repo -> package/version -> symbols/usages)  |
|                                                                         |
|  Migration Knowledge Base  Policy Engine                                |
|  (patterns, outcomes)      (risk, gates, approvals, retry limits)       |
|                           |                                             |
|                           v                                             |
|                 Fleet Orchestrator / Job Queue                          |
|           (impact selection, dedupe, bounded concurrency)               |
+---------------------------+---------------------------------------------+
                            |
               only affected repositories
                            |
          +-----------------+------------------+
          |                 |                  |
          v                 v                  v
    +-----------+     +-----------+      +-----------+
    | Repo Job A|     | Repo Job B| ...  | Repo Job N|
    | isolated  |     | isolated  |      | isolated  |
    | worktree  |     | worktree  |      | worktree  |
    +-----+-----+     +-----+-----+      +-----+-----+
          |                 |                  |
          +-----------------+------------------+
                            |
                            v
                 Repository Migration Engine
       Inspect -> Research -> Impact -> Plan -> Patch
                   -> Build/Test -> Repair
                            |
                            v
               Evidence + PR + Fleet Summary
```

**Key separation:** the Fleet Orchestrator decides *which repositories need work and when*; the Migration Engine decides *how to safely upgrade one repository*. Each job has isolated state and produces a typed result. Shared knowledge informs a job but never substitutes for its own validation.

### Hackathon implementation mapping

| Fleet component | Hackathon implementation | Long-term evolution |
|---|---|---|
| Release monitor | CLI/simulated release event; optional simple poll | Scheduled poller + webhooks + advisory feeds |
| Repository registry | YAML/JSON config or SQLite with 3 repositories | Persistent organization/team registry |
| Dependency/API index | Manifest inventory plus cached `rg` symbol index | Incremental semantic index keyed by commit/revision |
| Fleet orchestrator | Select affected repos; sequential or bounded async workers | Durable queue, resource-aware distributed workers |
| Migration knowledge | Small local store of reviewed pattern/result records; may be seeded for demo | Searchable, provenance-rich migration knowledge base |
| Policy engine | Small YAML config with safe defaults | Team/repo policies, approvals, compliance rules |
| Reporting | Per-repo reports + fleet summary | Fleet dashboard and analytics |

The P0 demo should show the architecture's value even if monitoring is triggered by a local event command rather than a continuously running daemon.

### Suggested repository layout

```text
depbot/
├── CLAUDE.md
├── instructions.md
├── plan.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── prompts/
│   ├── breaking_changes.md
│   ├── impact_classification.md
│   ├── migration_plan.md
│   ├── patch_generation.md
│   ├── patch_review.md
│   └── build_repair.md
│
├── src/depbot/
│   ├── __init__.py
│   ├── cli.py
│   ├── fleet/
│   │   ├── registry.py
│   │   ├── dependency_index.py
│   │   └── orchestrator.py
│   ├── monitor/
│   │   └── release_monitor.py       # event adapter; poller is post-hackathon/P1
│   ├── knowledge/
│   │   └── migration_store.py
│   ├── policy/
│   │   └── engine.py
│   ├── orchestrator.py
│   ├── state.py
│   ├── models.py
│   ├── llm/
│   │   ├── base.py
│   │   └── provider.py
│   ├── repository/
│   │   ├── inspector.py
│   │   ├── git_manager.py
│   │   ├── file_scanner.py
│   │   └── cmake_detector.py
│   ├── package_managers/
│   │   ├── base.py
│   │   ├── detector.py
│   │   ├── conan.py
│   │   └── vcpkg.py
│   ├── research/
│   │   ├── source_discovery.py
│   │   ├── github_client.py
│   │   ├── release_fetcher.py
│   │   ├── changelog_parser.py
│   │   ├── header_diff.py
│   │   └── breaking_change_analyzer.py
│   ├── code_intelligence/
│   │   ├── ripgrep_indexer.py
│   │   ├── symbol_extractor.py
│   │   ├── clang_analyzer.py
│   │   └── impact_analyzer.py
│   ├── migration/
│   │   ├── planner.py
│   │   ├── version_updater.py
│   │   ├── patch_generator.py
│   │   ├── patch_validator.py
│   │   └── reviewer.py
│   ├── verification/
│   │   ├── command_runner.py
│   │   ├── dependency_installer.py
│   │   ├── build_runner.py
│   │   ├── test_runner.py
│   │   ├── error_classifier.py
│   │   └── repair_engine.py
│   └── reporting/
│       ├── evidence_collector.py
│       ├── report_generator.py
│       ├── fleet_report.py
│       └── pull_request.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
│       ├── toy_dependency/
│       ├── toy_cpp_project_a/
│       ├── toy_cpp_project_b/
│       └── toy_cpp_project_c/
│
└── artifacts/                 # generated locally; gitignored except .gitkeep
```

This is the intended organization, not a requirement to create every file at hour 0. Start with the smallest runnable skeleton and add modules when needed.

---

## 5. Contracts and data flow

Agree on these contracts before splitting work. All modules should exchange typed Python objects, not free-form paragraphs.

### 5.1 Core models (`models.py`)

Use Pydantic models (or dataclasses if Pydantic is unavailable) for:

```python
class Dependency(BaseModel):
    name: str
    current_version: str
    target_version: str
    package_manager: str
    manifest_path: str
    is_direct: bool = True

class RepositoryRecord(BaseModel):
    repository_id: str
    repo_url_or_path: str
    display_name: str
    default_branch: str | None = None
    owner: str | None = None
    policy_id: str | None = None
    last_scanned_commit: str | None = None
    enabled: bool = True

class ReleaseEvent(BaseModel):
    event_id: str
    dependency_name: str
    upstream_repo: str
    previous_version: str | None = None
    target_version: str
    event_type: str = "release"  # release, security_advisory, deprecation
    source_url: str | None = None
    detected_at: str

class RepositoryDependencyRecord(BaseModel):
    repository_id: str
    dependency_name: str
    current_version: str
    package_manager: str
    manifest_path: str
    indexed_commit: str | None = None
    affected_symbols: list[str] = []

class MigrationPattern(BaseModel):
    pattern_id: str
    dependency_name: str
    version_range: str
    old_symbols: list[str]
    replacement_summary: str
    evidence_urls: list[str]
    context_notes: str
    verification_status: str  # verified, partial, failed
    source_run_id: str

class UpgradePolicy(BaseModel):
    policy_id: str
    allowed_change_levels: list[str] = ["patch", "minor"]
    require_tests: bool = True
    require_release_build: bool = False
    require_human_approval_for_major: bool = True
    minimum_migration_confidence: float = 0.80
    max_repair_attempts: int = 2
    create_pr_on_failure: bool = False
    auto_merge: bool = False

class BreakingChange(BaseModel):
    id: str
    title: str
    change_type: str          # removed, renamed, signature, behavior, deprecated, unknown
    affected_symbols: list[str]
    old_behavior: str | None = None
    new_behavior: str | None = None
    migration_advice: str
    evidence_urls: list[str]
    evidence_excerpt: str | None = None
    confidence: float         # 0.0–1.0

class CodeUsage(BaseModel):
    symbol: str
    file_path: str
    line_start: int
    line_end: int
    snippet: str
    analysis_method: str      # ripgrep, clang_ast, manual
    impact: str               # definite, possible, unaffected, unknown
    related_change_ids: list[str]
    confidence: float

class PlannedChange(BaseModel):
    file_path: str
    reason: str
    related_change_ids: list[str]
    expected_change: str
    risk: str

class CommandResult(BaseModel):
    command_label: str
    exit_code: int | None
    timed_out: bool = False
    stdout_path: str | None = None
    stderr_path: str | None = None
    duration_seconds: float

class VerificationResult(BaseModel):
    dependency_install: CommandResult | None = None
    configure: CommandResult | None = None
    build: CommandResult | None = None
    tests: CommandResult | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    baseline_summary: dict = {}
    upgraded_summary: dict = {}
    passed: bool = False

class DepBotState(BaseModel):
    run_id: str
    original_repo: str
    worktree_path: str
    base_branch: str
    working_branch: str
    dependency: Dependency
    repo_facts: dict = {}
    upstream_repo: str | None = None
    breaking_changes: list[BreakingChange] = []
    usages: list[CodeUsage] = []
    migration_plan: list[PlannedChange] = []
    changed_files: list[str] = []
    command_results: list[CommandResult] = []
    verification: VerificationResult | None = None
    report_path: str | None = None
    pr_url: str | None = None
    status: str = "initialized"
```

Use `Field(default_factory=list)` / `Field(default_factory=dict)` for mutable Pydantic defaults in actual code (including the list fields sketched above). Prefer enums/Literal types for statuses and event types in the implementation.

### 5.2 Fleet-level contracts

The hackathon should implement at least the following minimal interfaces, even if the backing store is a local JSON file or SQLite:

```python
class RepositoryUpgradeJob(BaseModel):
    event: ReleaseEvent
    repository: RepositoryRecord
    dependency_record: RepositoryDependencyRecord
    policy: UpgradePolicy

class RepositoryUpgradeOutcome(BaseModel):
    repository_id: str
    status: str  # skipped, unaffected, verified, blocked, failed
    reason: str | None = None
    run_id: str | None = None
    pr_url: str | None = None
    verification_passed: bool = False

class FleetRunSummary(BaseModel):
    event_id: str
    repositories_considered: int
    repositories_affected: int
    repositories_skipped: int
    repositories_verified: int
    repositories_failed_or_blocked: int
    outcomes: list[RepositoryUpgradeOutcome]
```

The registry/index must be able to answer: which registered repositories use dependency D, at what version, and which indexed symbols/usages may be affected by this release? Treat cached index results as candidate selection only; re-check manifests and source in the isolated current worktree before editing.

### 5.3 Stage contracts

| Stage | Input | Output | Stop conditions |
|---|---|---|---|
| Fleet event | release event + registry | deduplicated event and candidate repository IDs | invalid/duplicate event |
| Fleet impact | event + dependency/API index | affected/unaffected/already-upgraded repository classification | stale or ambiguous inventory; re-scan candidates |
| Schedule | affected repo jobs + policy | bounded worker jobs | concurrency/resource/policy limits |
| Inspect | repo, dependency, target | repo facts, package-manager adapter, current version, test/build commands, baseline result | unsupported repo/manager; dirty or unsafe repository; ambiguous dependency |
| Research | dependency + version range + upstream source | sourced `BreakingChange` list and research coverage | unknown upstream; target release unavailable; no evidence (may continue only with explicit uncertainty) |
| Impact | breaking changes + source tree | `CodeUsage` records linked to change IDs | no matching usages is valid; missing source files is not |
| Plan | breaking changes + usages + manifest | ordered `PlannedChange` list | unsupported or contradictory migration advice |
| Apply | plan + approved files | applied unified diffs + changed file list | patch invalid, out-of-scope file, or unsafe path |
| Verify | worktree + build/test config | install/configure/build/test result and logs | timeout, unresolved install/build/test failure, or retry limit |
| Deliver | verified state + evidence | report, commit, PR URL | verification gate not met or missing GitHub permission |

### 5.4 Orchestrator behavior

Implement explicit stage transitions and status values. Suggested statuses:

`INITIALIZED → INSPECTED → RESEARCHED → IMPACT_ANALYZED → PLANNED → PATCHED → VERIFIED → PR_CREATED`

Failure statuses:

`BLOCKED`, `FAILED_RESEARCH`, `FAILED_MIGRATION`, `FAILED_INSTALL`, `FAILED_BUILD`, `FAILED_TESTS`, `FAILED_PR`.

The repair loop transitions from `FAILED_BUILD` or `FAILED_TESTS` back to `IMPACT_ANALYZED` or `PLANNED`, with a strict attempt counter. Persist a run manifest and logs under `artifacts/<run_id>/` so a failed run can be diagnosed.

---

## 6. Stage-by-stage implementation details

### Stage 0 — Release event and fleet selection

**Files (suggested):** `monitor/release_monitor.py`, `fleet/registry.py`, `fleet/dependency_index.py`, `fleet/orchestrator.py`, `policy/engine.py`.

1. Accept a normalized `ReleaseEvent` from a CLI command or fixture. A simple scheduled poller is optional; do not make a daemon a P0 blocker.
2. Load registered repositories and their last known dependency inventories.
3. Query the index for repositories that use the dependency in the affected version range.
4. Classify repositories as `affected_candidate`, `unaffected`, `already_at_target_or_newer`, `disabled`, or `needs_rescan`.
5. Re-scan candidate repositories whose index is stale or whose manifest/commit has changed.
6. Apply policy before creating migration jobs.
7. Schedule only eligible candidates, with bounded concurrency and a sequential fallback.
8. Persist the event, per-repository outcomes, and fleet summary.

**Critical efficiency property:** do not launch an LLM migration workflow for every registered repository. Use deterministic dependency/version/API inventory to narrow the candidate set first. LLM calls are reserved for release interpretation, uncertain impact classification, migration planning, and repair.

### Stage 1 — Repository inspection

**Files:** `repository/inspector.py`, `file_scanner.py`, `cmake_detector.py`, `git_manager.py`, `package_managers/*`.

1. Accept a local path or clone a Git URL.
2. If a local path is dirty, stop by default. Do not stash or reset user changes automatically.
3. Create an isolated worktree/clone and branch `depbot/<dependency>-<target-version>`.
4. Detect CMake, package manager manifests, source directories, test directories, presets, and compile database.
5. Identify the requested direct dependency and current version. If multiple declarations or ambiguous versions exist, stop and report them.
6. Detect the baseline configure/build/test commands. Allow explicit command overrides for unusual projects.
7. Run baseline install/build/tests before modifying files. Record pre-existing failures.
8. Save repository facts to `artifacts/<run_id>/inspection.json`.

**Implementation notes:**
- Use `pathlib` and safe path resolution; reject paths escaping the worktree.
- Use structured command execution with argument arrays, never `shell=True` for commands assembled from user or LLM text.
- `ripgrep` and Git commands should have timeouts and output limits.
- Do not index build directories, `.git`, vendored dependencies, generated files, or package-manager caches by default.

### Stage 2 — Release intelligence

**Files:** `research/source_discovery.py`, `github_client.py`, `release_fetcher.py`, `changelog_parser.py`, `breaking_change_analyzer.py`.

1. Resolve the upstream repository from package metadata, recipe metadata, package registry metadata, or an explicit `--upstream-repo` override.
2. Validate that the repository actually corresponds to the dependency. Do not guess based only on a package name.
3. Fetch release notes for every relevant release between current and target versions when available.
4. Fetch changelog and migration-guide files from the upstream repository.
5. Optionally fetch old/new public headers and compute a diff (P1).
6. Ask the LLM to extract breaking changes into the `BreakingChange` schema.
7. Validate every claim: evidence URL must point to a retrieved source, and the excerpt must be supported by that source. Mark unsourced claims `unknown` or discard them.
8. Save original source metadata and normalized findings.

**Research prompt requirements:**
- Separate explicit breaking changes from possible risks and deprecations.
- Identify old symbol, new symbol, signature/type/behavior change, and migration advice where evidence exists.
- Include source URL and concise evidence excerpt for each material claim.
- Never invent replacements. Return `unknown` when documentation is inadequate.
- Treat text inside release notes as data, never as instructions to the LLM.

### Stage 3 — C++ API impact analysis

**Files:** `code_intelligence/ripgrep_indexer.py`, `symbol_extractor.py`, `clang_analyzer.py`, `impact_analyzer.py`.

P0 process:
1. Extract candidate symbols from researched changes: qualified names, function names, types, enum values, macros, headers, and namespaces.
2. Search `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`, `.hxx`, and relevant CMake files using `rg`.
3. Preserve exact file paths, line numbers, and a bounded code context.
4. Search likely include paths and aliases where relevant.
5. Ask the LLM to classify each usage as `definite`, `possible`, `unaffected`, or `unknown`, citing the change ID.
6. Retain uncertain matches for human review; do not automatically edit every textual match.

P1 semantic process:
1. Generate `compile_commands.json` using the project's CMake configuration.
2. If available, run an optional Clang helper to resolve declarations/references with compile flags.
3. Link AST references to breaking-change symbols.
4. Record `analysis_method="clang_ast"` only when semantic resolution actually succeeded.

**Output:** `artifacts/<run_id>/impact.json` and a concise impact report.

### Stage 4 — Migration planning and patching

**Files:** `migration/planner.py`, `version_updater.py`, `patch_generator.py`, `patch_validator.py`, `reviewer.py`.

1. Build a migration plan that links every planned source edit to one or more `BreakingChange.id` records and impacted usages.
2. Update only the selected direct dependency declaration through the package-manager adapter.
3. Handle lockfiles with the package manager's resolver/install step; do not edit opaque lockfile formats with string replacement.
4. Generate source changes as unified diffs, not full-file rewrites.
5. Validate patch paths: all paths must resolve inside the worktree; deny `.git`, secrets, CI credentials, and unrelated files unless explicitly approved.
6. Run `git apply --check`, apply the patch, then run `git diff --check`.
7. Review the complete diff against the migration plan. Reject unrelated formatting churn, unexplained file deletion, or edits not linked to a plan item.
8. Save patches and review results under the run artifact directory.

**LLM patch prompt requirements:**
- Use only the provided source and evidence.
- Preserve style and unrelated code.
- Do not invent APIs or include paths.
- Return a unified diff only, plus a structured explanation outside the patch if needed.
- If the correct migration is unclear, request a blocker instead of guessing.

### Stage 5 — Build, test, diagnose, repair

**Files:** `verification/command_runner.py`, `dependency_installer.py`, `build_runner.py`, `test_runner.py`, `error_classifier.py`, `repair_engine.py`.

Suggested sequence:

1. Install/resolve the upgraded dependency with the selected package manager.
2. Configure with CMake using the repository's presets/options or the package manager's generated toolchain.
3. Build the default configuration.
4. Run CTest (or the repository's detected test command).
5. Compare results against the baseline.
6. If a failure is plausibly caused by the upgrade, classify diagnostics and attempt a targeted repair.
7. Re-run the minimum relevant checks, then run the full build/test gate again.
8. Stop after 2–3 repair attempts, repeated identical errors, timeouts, or unrelated/environmental failures.

Optional P1 checks after the main gate passes:
- Release build in addition to Debug/default.
- `clang-tidy` on changed translation units.
- ASan/UBSan build where compatible.
- Additional project-specific test targets.

**Error classification:**
- Dependency resolution/install failure.
- CMake configure/generator failure.
- Missing header or include change.
- Removed/renamed symbol.
- Function signature or type mismatch.
- Template instantiation error.
- Linker/ABI error.
- Test assertion / behavioral regression.
- Pre-existing baseline failure.
- Environment/network/timeout failure.

Do not ask the repair model to solve all errors in one giant prompt. Pass the smallest relevant diagnostic excerpt, affected source context, compile command when available, and linked migration evidence. Every repair is another patch subject to the same validation and review rules.

### Stage 6 — Evidence and PR delivery

**Files:** `reporting/evidence_collector.py`, `report_generator.py`, `pull_request.py`.

Generate `artifacts/<run_id>/upgrade-report.md` with:

- Dependency, old version, target version, package manager, and build configuration.
- Upstream sources consulted.
- Breaking changes with evidence links.
- Affected symbols and file/line locations.
- Migration plan and files modified.
- Summary of the final diff.
- Actual dependency install/configure/build/test commands, exit codes, durations, and test counts.
- Baseline failures and how they compare with upgraded results.
- Repair attempts and their outcomes.
- Remaining risks, unsupported areas, and manual follow-up.

Create a PR only when the verification policy allows it. Prefer `gh pr create` with a generated body file, or a small GitHub API client. Never put tokens into logs, model prompts, command output, or the report.

---

## 7. CLI and configuration

### Example CLI

```bash
# Register repositories for autonomous maintenance
python -m depbot fleet register --config depbot-fleet.yaml

# Process a normalized upstream release event across the registered fleet
python -m depbot fleet process-release \
  --dependency fmt --from 10.2.1 --to 11.0.2 \
  --event-source fixture --create-pr

# Inspect fleet inventory and the latest event summary
python -m depbot fleet status

# Optional long-term entry point: run scheduled release polling
python -m depbot monitor --config depbot-fleet.yaml

# Targeted single-repository upgrade (same migration engine)
python -m depbot upgrade \
  --repo ./sample-cpp-project --dependency fmt --to 11.0.2

# Local repository
python -m depbot upgrade \
  --repo ./sample-cpp-project \
  --dependency fmt \
  --to 11.0.2

# GitHub repository + PR
python -m depbot upgrade \
  --repo https://github.com/org/project \
  --dependency fmt \
  --to 11.0.2 \
  --base main \
  --create-pr

# Explicit upstream if package metadata is ambiguous
python -m depbot upgrade \
  --repo ./sample-cpp-project \
  --dependency example-lib \
  --to 3.0.0 \
  --upstream-repo owner/example-lib
```

The versions above are examples only. The actual demo upgrade must be selected and validated before the hackathon.

### Configuration

Support command-line options plus a small `depbot.yaml` / `depbot-fleet.yaml` or `pyproject.toml` section for:

- registered repositories and their identifiers/owners;
- release polling interval or event source (optional for the hackathon);
- worker concurrency and resource limits;
- policy profile per repository/team;
- registry/index/knowledge-store path (SQLite or JSON for the MVP);
- build/configure/test command overrides;
- configure preset/build type;
- timeout and output limits;
- maximum repair attempts;
- whether to run optional static analysis/sanitizers;
- PR creation and base branch;
- allowed editable directories.

Environment variables belong in `.env` locally and must never be committed:

```text
LLM_API_KEY=
GITHUB_TOKEN=
```

Use least-privilege GitHub permissions. Only the PR publishing stage should receive the publishing credential.

---

## 8. Testing strategy

Testing must cover both DepBot's code and its ability to complete a migration.

### 8.1 Deterministic fixture

Create a small toy dependency with two local versions:

```cpp
// toydep v1
int old_sum(int a, int b);

// toydep v2
int sum(int a, int b);
```

The fixture application's source initially calls `old_sum`. The toy dependency's changelog explicitly states that `old_sum` was removed and replaced with `sum`.

Use this fixture to test research parsing, API-to-usage mapping, migration planning, patch application, CMake build, and tests. Keep the fixture deterministic and local so the end-to-end test does not require GitHub or an LLM call. Mock the research/LLM result for the deterministic test. Separately run at least one live migration against a real upstream library for the demo.

### 8.2 Three-repository fleet acceptance fixture

Create three small CMake repositories that share a toy dependency:

- **Repo A:** uses the removed v1 API and requires migration.
- **Repo B:** uses the same removed API through a different call site and requires migration; it can reuse a validated migration pattern as planning context.
- **Repo C:** uses an unaffected API, or is already on the target version, and must be skipped without a migration LLM call/build job.

Inject one fixture `ReleaseEvent` (toydep v1 → v2). Assert that the fleet selector chooses A and B, skips C for a grounded reason, executes repository jobs in isolated worktrees, verifies each migration independently, and emits two outcomes plus a fleet summary. The test should be deterministic and not require a live monitor, GitHub credentials, or live LLM calls. The demo may use local PR-like artifacts if credentials are unavailable, but must clearly distinguish them from actual GitHub PRs.

### 8.3 Unit tests

| Test area | Cases |
|---|---|
| Inspector | Detect CMake, manifests, requested dependency, dirty state, source/test directories |
| Conan adapter | Parse requirements, update only selected direct requirement, reject ambiguous/dynamic cases |
| vcpkg adapter | Parse manifest/version constraint and baseline; resolve target or refuse safely |
| Research parser | Parse releases, version range, source URLs, unknown claims |
| Breaking-change schema | Reject missing source/evidence fields and invalid confidence |
| Code search | Find symbol, path, line, snippets; ignore build/cache/vendor directories |
| Impact analyzer | Classify definite/possible/unaffected/unknown and preserve change IDs |
| Patch validator | Reject malformed patch, path traversal, unrelated file edits, failed `git apply --check` |
| Command runner | Capture output/exit code, enforce timeout, redact secrets, cap output |
| Error classifier | Classify representative compiler/linker/test errors |
| Report generator | Include only actual commands/results and supported claims |

### 8.4 Integration tests

- Inspector + package-manager adapter.
- Release parser + breaking-change schema.
- Breaking changes + code search + impact analyzer.
- Migration plan + patch generator + patch validator.
- Package install + CMake configure + build + CTest.
- Verification results + evidence report.
- Git branch/commit + PR creation using a disposable test repository (PR test may be manual if credentials are unavailable).

### 8.5 End-to-end acceptance test

```python
def test_toy_dependency_upgrade():
    result = run_upgrade(
        repo="tests/fixtures/toy_cpp_project",
        dependency="toydep",
        target_version="2.0.0",
        create_pr=False,
    )

    assert result.status == "VERIFIED"
    assert result.verification is not None
    assert result.verification.build.exit_code == 0
    assert result.verification.tests.exit_code == 0
    assert result.verification.passed is True
    assert "src/main.cpp" in result.changed_files
    assert result.report_path is not None
```

Adapt field names to the final models. The test should exercise the real local orchestrator and real CMake build; only external research and LLM outputs should be fixture-backed.

### 8.6 Negative tests

- Unknown dependency or target version.
- Upstream repository cannot be established.
- No release evidence found.
- Target version unavailable in package manager.
- Dependency is transitive rather than direct.
- No API usages found (valid outcome; still build/test).
- Patch fails validation or touches an out-of-scope path.
- Install/build/test times out.
- Baseline is already failing.
- Repair attempt repeats the same failure.
- GitHub credentials missing or PR creation fails.

Expected behavior is a clear blocked/failed status, preserved logs, and no false success claim.

---

## 9. Three-person team workflow

The team has three people. Split by stable module boundaries, not by individual workflow steps that require constant handoffs.

### Member A — Core, repository, package managers, integration

**Owns:**
- `models.py`, `state.py` (contracts; coordinate changes with everyone).
- `cli.py`, repository-scoped `orchestrator.py`.
- `fleet/registry.py`, `fleet/orchestrator.py`, minimal release-event ingestion and policy wiring.
- `repository/*`.
- `package_managers/*`.
- `reporting/pull_request.py` (or delegate final PR wiring if needed).
- Git/worktree safety and overall integration.

**Deliverables:** A runnable CLI, three-repository registry, event-to-job selection, isolated working copies, package version update, orchestrated stage transitions, branch/commit/PR path, fleet summary.

### Member B — Research and C++ code intelligence

**Owns:**
- `research/*`.
- `code_intelligence/*`.
- `prompts/breaking_changes.md` and `prompts/impact_classification.md`.

**Deliverables:** Upstream source discovery, release/changelog retrieval, normalized release event evidence, sourced breaking-change records, symbol extraction, source usage index and repository-level impact report. Seed or expose reusable migration-pattern metadata where useful.

### Member C — Migration, verification, fixtures and evidence

**Owns:**
- `migration/*`.
- `verification/*`.
- `tests/fixtures/*`, `tests/integration/*`, `tests/e2e/*`.
- `reporting/evidence_collector.py` and `report_generator.py`.
- `prompts/migration_plan.md`, `patch_generation.md`, `patch_review.md`, `build_repair.md`.

**Deliverables:** Validated unified patches, working build/test pipeline, error classification, bounded repair loop, deterministic fixture, evidence report.

### Shared ownership rules

- Member A is the integration owner and controls `models.py`, `state.py`, `orchestrator.py`, and CLI contracts.
- Members B and C must not independently change shared models. Propose a contract change to A and coordinate it before implementation.
- Each member works on a separate Git branch/worktree.
- No one edits another member's owned files without a quick agreement.
- Every module owner supplies a short usage example and tests with the module.
- Integrate small commits frequently; do not wait until hour 10 for a giant merge.

### Communication cadence

At each checkpoint, each member reports:

1. What is working (with command/test evidence).
2. What interface/output is ready for another module.
3. What is blocked.
4. Which files or interfaces changed.

Use the shared Pydantic contracts as the integration boundary.

---

## 10. Twelve-hour schedule for three people

| Time | Member A — Core | Member B — Intelligence | Member C — Migration/verification | Integration milestone |
|---|---|---|---|---|
| 00:00–00:45 | Repo skeleton, fleet/job contracts, branch strategy | Review release-event and research schemas | Build three toy repos and verification requirements | Agree models, event schema, CLI arguments, file ownership |
| 00:45–02:00 | CLI, minimal registry, event ingestion, worktree/inspector | GitHub client, release fetch/parser | Patch validator, command runner, three-repo fixture | Registry → event → candidate selection runs |
| 02:00–04:00 | Dependency inventory, selective fleet scheduler, Conan adapter; vcpkg if time | Upstream discovery, breaking-change extraction, ripgrep index | Migration planner and package version patch tests | Event selects affected repos and skips unaffected repo |
| 04:00–05:30 | Per-repo job orchestration and shared state | Impact classification; optional Clang spike | Unified patch generation and review | First repo migration patch produced |
| 05:30–07:00 | Package resolution, isolated repo jobs, fleet summary | Header diff / AST only if core analysis is stable | CMake install/build/CTest pipeline | **Feature freeze: three-repo selection + one complete local migration path** |
| 07:00–08:30 | Integrate adapters, bounded concurrency/sequential fallback, Git | Fix source discovery/impact edge cases | Error classifier and 1–2 repair attempts; migration pattern record | Three-repo fleet fixture passes end to end |
| 08:30–09:30 | PR integration and branch push | Research evidence review | Evidence collector and report | PR-ready report with real logs |
| 09:30–10:30 | Fix fleet/repo orchestration bugs | Run a real dependency research pass | Run real migration and verification | **Fleet demo plus one real repository upgrade succeeds locally** |
| 10:30–11:15 | PR creation test | Audit migration evidence and changed usages | Add missing regression/negative tests | **Actual PR or documented credential blocker** |
| 11:15–12:00 | Demo reliability, README, final merge | Demo narrative and findings | Demo report, logs, fallback fixture | Freeze code and rehearse live demo |

### Hard checkpoints

- **45 minutes:** contracts and ownership are agreed.
- **2 hours:** skeleton and fixture run; no one is coding against undefined interfaces.
- **4 hours:** research and impact outputs exist.
- **5.5 hours:** a patch can be generated.
- **7 hours:** feature freeze; local vertical slice must work.
- **9.5 hours:** end-to-end run must be integrated.
- **10.5 hours:** stop adding features; focus on reliable demo and PR.

If a checkpoint slips, drop P1 work (Clang AST, header diff, extra build configurations) before sacrificing the verified end-to-end path.

---

## 11. Using Claude Code efficiently as a three-person team

Claude Code is an implementation accelerator. It should not become a second source of architectural decisions.

### Initial setup

1. Put `CLAUDE.md`, `instructions.md`, and `plan.md` in the repository root.
2. Start each Claude Code session in the repository root.
3. Ask Claude Code to read the project instructions and the specific module contract before coding.
4. Give each Claude Code session a bounded task and explicit file ownership.
5. Ask it to run targeted tests and report exact files changed and commands executed.

### Parallel work

- Use one Git branch/worktree per human owner.
- If using multiple Claude Code sessions, keep each session inside its owner's branch/worktree.
- Do not have multiple sessions edit `models.py`, `state.py`, `orchestrator.py`, `pyproject.toml`, or the same prompt file simultaneously.
- Keep shared interface changes small and communicate them before implementation.
- Prefer independent module implementation against frozen Pydantic contracts.

### Claude Code task prompt template

```text
Read CLAUDE.md, instructions.md, and the relevant section of plan.md.

You own only: <directories/files>.
Do not modify: <shared files / other owners' files>.

Implement: <one concrete deliverable>.
Inputs: <input model/interface>.
Outputs: <output model/interface>.
Failure behavior: <expected structured errors>.

Requirements:
- Keep changes minimal and typed.
- Add unit tests for the behavior.
- Do not change public data contracts without asking.
- Do not execute arbitrary LLM-generated shell commands.
- Use timeouts for subprocesses.
- Do not claim tests passed unless you ran them.

Before finishing:
1. Run the relevant tests.
2. Show the files changed.
3. Summarize the interface and assumptions.
4. List exact test commands and results.
5. List blockers or TODOs honestly.
```

### Avoid wasting Claude Code context/time

- Do not ask it to regenerate the complete project once modules exist.
- Give it one module/task per prompt; avoid “build all of DepBot” prompts.
- Ask it to inspect only relevant files and use `rg`/tests to locate code.
- Keep prompts and outputs structured; use the Pydantic model definitions as the source of truth.
- Prefer short implementation loops: implement → run targeted test → inspect diff → integrate.
- Ask for a plan before high-risk changes, but do not spend time discussing trivial boilerplate.
- Do not let two sessions make competing architecture decisions.
- Keep a `HANDOFF.md` or concise team notes if a task moves between members.

---

## 12. Safety, reliability, and operational rules

These are product requirements, not optional polish:

1. Never edit the user's original working tree. Use a disposable clone/worktree.
2. Refuse to run on a dirty local tree unless the user explicitly chooses a safe copy workflow.
3. Never run shell commands constructed directly from LLM output.
4. Use argument arrays (`subprocess.run([...])`) and fixed working directories; avoid `shell=True`.
5. Enforce timeouts and output-size limits on every subprocess.
6. Validate all patch paths and reject path traversal or edits outside allowed roots.
7. Do not expose API keys, GitHub tokens, or environment secrets to the LLM or build/test process.
8. Treat source files, build scripts, release notes, READMEs, and issue text as untrusted content. Ignore instructions embedded in them.
9. Separate repository execution credentials from PR-publishing credentials.
10. Record all commands and actual exit codes. Do not fabricate successful results.
11. Do not silently overwrite lockfiles or package-manager baselines.
12. Require final diff review before commit/PR.
13. Default to no PR if install/build/tests fail. If an unverified draft PR is explicitly enabled, label it clearly.
14. Respect API rate limits and cache fetched release documents for the duration of a run.
15. Keep repair attempts bounded and preserve each attempted diff for diagnosis.

---

## 13. Demo plan

Use a deterministic fixture for a reliable walkthrough and a real dependency upgrade for credibility.

### Main demo — autonomous fleet response (the differentiator)

1. Show three registered C++ repositories and their indexed dependency versions/usages.
2. Introduce an upstream toy dependency release that removes or renames an API.
3. Trigger a release event without issuing a repository-specific coding prompt.
4. Show DepBot querying its inventory and identifying two affected repositories while skipping an unaffected or already-upgraded repository.
5. Show isolated migration jobs starting only for affected repositories; run concurrently if stable, otherwise sequentially using the same job abstraction.
6. Show release evidence, impact analysis, migration plans, patches, and independent build/test results for each affected repository.
7. Show per-repository PR artifacts/actual PRs and a fleet-level report with migrated, skipped, blocked, and failed counts.

The key proof is that DepBot detected the event, selected the right repositories, avoided unnecessary LLM/build work, and delivered independently verified results without a developer prompt.

### Demo A — deterministic single-repository fallback

1. Show a toy C++ application pinned to toy dependency v1.
2. Show the toy dependency changelog documenting an API removal/rename.
3. Run the repository-scoped migration engine directly.
4. Show the affected file/line, migration plan, diff, build and tests.
5. Show the generated upgrade report.

This offline fallback must work even if fleet orchestration or release event integration breaks.

### Demo B — real repository (secondary credibility demo)

Before the hackathon, select one small public C++ repository and one dependency upgrade. Verify that:

- the old and target versions exist;
- upstream release notes document a meaningful API change;
- the app actually uses the affected API;
- the package manager can resolve the target version;
- baseline tests run;
- a working migration is achievable.

Do not choose the real demo dependency on the day of the hackathon. Preflight it and keep a second candidate ready.

### Final demo sequence

- Show the registered repository fleet and dependency inventory.
- Trigger one upstream release event (no repository-specific prompt).
- Show affected-repository selection and why unaffected repositories were skipped.
- Show isolated migration jobs and shared migration knowledge used as context.
- Open each final diff and show actual build/test evidence.
- Show per-repository PRs (or clearly labelled local PR artifacts) and the fleet summary.
- Briefly show a negative case or policy gate to demonstrate that DepBot handles uncertainty/failure honestly.

---

## 14. Final acceptance checklist

### Product workflow
- [ ] Three repositories can be registered and inventoried.
- [ ] A release event is ingested/deduplicated without a repository-specific prompt.
- [ ] Fleet selection distinguishes affected, unaffected, and already-upgraded repositories.
- [ ] Only affected/eligible repositories receive migration jobs.
- [ ] Each repository job uses an isolated worktree and independent verification.
- [ ] Per-repository outcomes and an aggregate fleet report are generated.
- [ ] CLI also accepts a targeted local repo or remote URL, dependency, target version, and optional PR flag.
- [ ] Repository is isolated; original checkout is unchanged.
- [ ] CMake project and package manager are detected.
- [ ] Current and target versions are validated.
- [ ] Upstream source is identified and release evidence is stored.
- [ ] Breaking changes have source references and confidence.
- [ ] Affected usages have file/line and analysis method.
- [ ] Migration plan links each code edit to an upstream change.
- [ ] Manifest and source changes are validated patches.
- [ ] Build and tests run and their logs/exit codes are stored.
- [ ] Repair loop is bounded and re-verifies changes.
- [ ] Report contains only claims supported by evidence.
- [ ] PR is opened only according to verification policy.

### Engineering quality
- [ ] Unit tests pass.
- [ ] Deterministic three-repository fleet end-to-end fixture passes.
- [ ] Single-repository fallback migration passes.
- [ ] At least one real dependency upgrade works locally.
- [ ] No secrets in repository, artifacts, prompts, or logs.
- [ ] No accidental modifications outside the isolated worktree.
- [ ] Demo has a known-good fallback path.

---

## 15. Long-term roadmap after the hackathon

The following sequence grows the hackathon prototype into the full DepBot product. It is intentionally separate from the 12-hour acceptance criteria.

**Phase 1 — Reliable fleet core:** persistent registry; scheduled release polling/webhooks; incremental manifest and API indexing; durable event/job records; idempotency and retry semantics; bounded parallel workers; per-repository policy configuration.

**Phase 2 — Accumulated migration intelligence:** provenance-rich migration knowledge base; similarity retrieval over prior migrations; cross-repository pattern validation; failure taxonomy and repair analytics; confidence calibration from actual outcomes.

**Phase 3 — Organization-scale maintenance:** GitHub App installation and repository discovery; team ownership and approval workflows; security advisory prioritization; fleet dashboard; metrics for coverage, verified success, false-positive impact selection, cost, and time saved.

**Phase 4 — Deeper C++ semantics and ecosystems:** persistent Clang/AST index; ABI and behavioral compatibility analysis; broader build systems/package managers; monorepo-aware dependency graph; coordinated multi-dependency upgrade planning.

The moat is the accumulated, verified organization-wide dependency/API knowledge and operational workflow—not a claim that DepBot's LLM is intrinsically better at editing code than Claude Code.

## 16. What to cut first if time runs short

Cut in this order:

1. Clang AST / LibTooling integration.
2. Header-level upstream API diff.
3. vcpkg automatic baseline changes (keep safe detection/refusal; do not fake support).
4. Release build, sanitizers, and static analysis.
5. Fancy terminal UI or web dashboard.
6. Advanced repair beyond one targeted attempt.

Do **not** cut:

- isolated repository workflow;
- evidence-backed breaking-change analysis;
- patch validation;
- actual build and test verification;
- accurate failure reporting;
- deterministic end-to-end fixture.

The winning demo is one real upgrade that DepBot can explain and verify—not the largest feature checklist.
