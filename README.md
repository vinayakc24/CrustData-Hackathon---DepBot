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

toydep 2.0.0 removes `old_sum` in favour of `sum` (see its `CHANGELOG.md`).

```bash
# 1. Build toydep 1.0.0 and 2.0.0 into an isolated, offline Conan home
#    (examples/.depbot/conan; your ~/.conan2 is never touched)
depbot demo setup --fleet examples/demo-fleet.yaml \
  --recipe tests/fixtures/toydep/1.0.0 --recipe tests/fixtures/toydep/2.0.0

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

### Single-repository fallback

```bash
depbot upgrade --repo tests/fixtures/repos/repo_a --dependency toydep --to 2.0.0 \
  --changelog tests/fixtures/toydep/2.0.0/CHANGELOG.md \
  --conan-home examples/.depbot/conan --approve-major
```

### LLM provider

Bounded LLM tasks (release interpretation, uncertain impact classification) use the configured provider:

```bash
--llm none                                   # default: deterministic stages only
--llm fixture --llm-fixtures demo-llm.json   # recorded responses, offline and reproducible
--llm anthropic [--llm-model MODEL]          # needs LLM_API_KEY (or ANTHROPIC_API_KEY)
```

The API key is used only in the provider's request header. It is never passed to builds,
prompts or logs.

### GitHub PRs

`--create-pr` (on `run` or `upgrade`) pushes the verified branch to the
source repository's GitHub `origin` and runs `gh pr create`. That happens only
after the pre-PR policy gate passes, and it never auto-merges.
Without `--create-pr`, or for repositories without a GitHub remote, DepBot
writes a clearly labelled local PR artifact instead.

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
