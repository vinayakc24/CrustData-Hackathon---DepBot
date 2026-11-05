# DepBot

Fleet-wide, evidence-backed C++ dependency migration. One upstream release
event goes in. DepBot picks the registered repositories it actually affects,
migrates each one in an isolated working copy, runs the real
install/configure/build/test, and reports per repository plus one fleet summary.

- Product scope and architecture: [`plan.md`](plan.md)
- Rules for contributors and Claude Code sessions: [`instructions.md`](instructions.md)
- Shared contracts and stage signatures (Member A owns): [`CONTRACTS.md`](CONTRACTS.md)

## Requirements

Python 3.11+, CMake ≥ 3.15, a C++17 compiler, Git, ripgrep, and a package manager:
Conan 2 (`conanfile.txt`) or vcpkg in manifest mode (`vcpkg.json`, with `VCPKG_ROOT` set).
`gh` is needed only for `--create-pr`.

```bash
pip install -e ".[dev]"
pytest
```

## Demo: one release event across a three-repository fleet

The fleet (`examples/demo-fleet.yaml`) registers three CMake + Conan repos
that depend on the local toy library `toydep`:

| Repo | Uses | Expected |
|---|---|---|
| `repo_a` | `old_sum` in `src/main.cpp`, toydep 1.0.0 | migrated + verified |
| `repo_b` | `old_sum` at another call site, toydep 1.0.0 | migrated + verified |
| `repo_c` | already on toydep 2.0.0 | skipped, no job/LLM/build |

toydep 2.0.0 removes `old_sum` in favour of `sum` (see `tests/fixtures/toydep/release-notes-2.0.0.md`).

```bash
# 1. Build toydep 1.0.0 and 2.0.0 into an isolated, offline Conan home
#    (examples/.depbot/conan; your ~/.conan2 is never touched)
depbot demo setup --fleet examples/demo-fleet.yaml \
  --recipe tests/fixtures/toydep/v1 --recipe tests/fixtures/toydep/v2

# 2. Inspect the registered fleet and its dependency inventory
depbot fleet status --fleet examples/demo-fleet.yaml

# 3. Dry run: which repositories would get a job, and why the others are skipped
depbot fleet plan --fleet examples/demo-fleet.yaml \
  --event examples/events/toydep-v2.yaml --approve-major

# 4. Process the release event across the fleet
depbot run --fleet examples/demo-fleet.yaml \
  --event examples/events/toydep-v2.yaml --approve-major
```

`toydep` 1.0.0 → 2.0.0 is a **major** upgrade. Without `--approve-major`,
policy blocks both affected repositories; that is a useful policy-gate demo in itself.
Re-running the same event is refused as a duplicate. Add `--force` to reprocess it.

### Where the evidence lands

```text
artifacts/
├── fleet-toydep-v2-<stamp>/
│   ├── selection.json        # every repo's classification and reason
│   ├── summary.json          # FleetRunSummary
│   └── summary.md            # human-readable fleet report
└── toydep-v2-repo_a-<stamp>/
    ├── worktree/             # isolated copy (branch depbot/toydep-2.0.0)
    ├── inspection.json  breaking_changes.json  impact.json  plan.json
    ├── patches/              # manifest.diff, each source patch, final.diff
    ├── pr/                   # pr-body.md + pr.json (local PR artifact)
    ├── state.json            # full job state, status history, policy decisions
    └── upgrade-report.md     # per-repository report
```

### Registering GitHub repositories

`repo_url_or_path` can be a local path **or** a Git URL (`https://…`, `ssh://…`, `git@host:owner/repo`):

```yaml
repositories:
  - repository_id: service_x
    display_name: Service X
    repo_url_or_path: https://github.com/acme/service-x
    default_branch: main        # optional; defaults to the remote's HEAD branch
```

On every `fleet status` / `plan` / `run`, DepBot clones the repository once into
`<state_dir>/sources/<id>-<hash>/`, or fetches it on later runs, and checks out the tip of the branch.
Each job then makes its isolated working copy from that cache, and `--create-pr` pushes to its GitHub `origin`.
Access uses your existing `git`/`gh` credentials. URLs with embedded credentials
(`https://user:token@…`) are refused, and nothing is ever pushed except an explicit `--create-pr` branch.
`depbot upgrade --repo https://github.com/acme/service-x …` works the same way.

### Watching upstream releases (`depbot monitor`)

Add the upstream projects to watch to the fleet config:

```yaml
watches:
  - dependency_name: fmt          # as declared in conanfile.txt / vcpkg.json
    upstream_repo: fmtlib/fmt     # GitHub owner/name
    tag_prefix: ""                # e.g. "fmt-" if release tags look like fmt-11.0.2
```

Set `DEPBOT_GITHUB_TOKEN` to a read-only token to raise GitHub's anonymous limit (60 requests/hour).
It is sent only to `api.github.com`, never on redirects, and is separate from the PR-publishing credential.

```bash
depbot monitor --fleet depbot-fleet.yaml --once --dry-run   # what would happen, nothing recorded
depbot monitor --fleet depbot-fleet.yaml --once             # one poll; process new releases
depbot monitor --fleet depbot-fleet.yaml --interval 3600    # keep polling (Ctrl-C to stop)
```

Each poll compares the lowest version any enabled repository declares with the upstream's latest stable
GitHub release. A newer release becomes a `ReleaseEvent`, which goes through the same path as
`depbot run`: duplicate check, selection, isolated jobs, fleet summary. A release already processed is
reported as a duplicate. A failing watch or fleet run doesn't stop the others. `run` options
(`--approve-major`, `--create-pr`, `--llm …`) apply. The minimum interval is 60 s.

### Single-repository fallback

```bash
depbot upgrade --repo tests/fixtures/toydep/repositories/repo_a --dependency toydep --to 2.0.0 \
  --changelog tests/fixtures/toydep/release-notes-2.0.0.md \
  --conan-home examples/.depbot/conan --approve-major
```

### LLM provider

Bounded LLM tasks (release interpretation, uncertain impact classification) use the configured provider:

```bash
--llm none                                   # default: deterministic stages only
--llm fixture --llm-fixtures demo-llm.json   # recorded responses, offline and reproducible
--llm anthropic [--llm-model MODEL]          # needs LLM_API_KEY (or ANTHROPIC_API_KEY)
--llm gemini [--llm-model MODEL]             # needs GEMINI_API_KEY
```

Gemini requests use Google's HTTPS Developer API and send `GEMINI_API_KEY` only
in the `x-goog-api-key` header. Both production providers use `--llm-model` or
`DEPBOT_LLM_MODEL` for model selection; Gemini defaults to `gemini-2.5-flash`.
Structured responses are requested as JSON and validated locally against the
stage's Pydantic schema.

The API key is used only in the provider's request header. It is never passed to builds,
prompts or logs.

### GitHub PRs

`--create-pr` (on `run` or `upgrade`) pushes the verified branch to the
source repository's GitHub `origin` and runs `gh pr create`. That happens only
after the pre-PR policy gate passes, and it never auto-merges.
Without `--create-pr`, or for repositories without a GitHub remote, DepBot
writes a clearly labelled local PR artifact instead.

## Demo B: a real upstream upgrade ({fmt} 6.2.1 → 7.0.0)

A service with a custom `fmt::formatter` breaks on fmt 7.0.0, which removed
`fmt::parse_context`. DepBot builds both real fmt releases offline, reads the upstream
changelog, confirms the usage with libclang, patches it and verifies with `ctest`.
Runbook: [`examples/real-fmt/README.md`](examples/real-fmt/README.md).

## How DepBot decides what changed and where

Deterministic layers settle everything they can prove before an LLM is consulted:

- **What changed:** R1 diffs the dependency's old and new public headers (libclang; `api_diff.json`).
  R2 parses the release notes for every version in the range. R3, the LLM, only sees what both left
  unexplained, and its findings are added, never replacing.
- **Where it hits:** I1 finds text matches (ripgrep). I2 resolves each one to the exact declaration it names
  (libclang). I3 uses compiler errors from a probe build against the new version. I4, the LLM, only sees
  what is still unresolved. Only AST- or compiler-proven usages are edited automatically.

`pip install -e ".[ast]"` enables the libclang layers (system libclang 18). Without it, DepBot falls back
to text matching.

## Safety guarantees

- The source checkout is never modified. Uncommitted changes are refused.
- Every external command uses an argv list, `shell=False`, a fixed `cwd` and a
  timeout. Builds get an allow-listed environment with no tokens or API keys.
- Patches can touch only files named in the migration plan. The dependency
  manifest is changed only through the package-manager adapter.
- vcpkg: only the target's `version>=`/override changes; `builtin-baseline` is never moved, and
  dependencies pinned only by the baseline are refused with an explanation.
- A repository is reported `verified` only if the upgraded install, configure,
  build and tests actually ran and passed.
- Repair attempts are bounded by policy, and an identical patch is never re-applied.

## Status

| Area | Owner | State |
|---|---|---|
| Contracts, CLI, registry (local + GitHub repos), selection, policy, Conan + vcpkg adapters, working copies, engine, fleet runner, PR delivery | A | implemented and unit-tested |
| Release research, code impact analysis | B | in progress |
| Fixtures, migration planning/patching, verification, repair, per-repo reports | C | in progress |

Until B's and C's stages are merged, `depbot run` reports affected repositories as
`BLOCKED: stage '<name>' not integrated yet`. It never reports a fake success.
