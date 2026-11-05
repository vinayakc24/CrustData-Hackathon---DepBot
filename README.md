# DepBot

Fleet-wide, evidence-backed C++ dependency migration. One upstream release
event goes in. DepBot picks the registered repositories it actually affects,
migrates each one in an isolated working copy, runs the real
install/configure/build/test, and reports per repository plus one fleet summary.

- Product scope and architecture: [`plan.md`](plan.md)
- Rules for contributors and Claude Code sessions: [`instructions.md`](instructions.md)
- Shared contracts and stage signatures (Member A owns): [`CONTRACTS.md`](CONTRACTS.md)

## Requirements

Python 3.11+, CMake ≥ 3.15, a C++17 compiler, Conan 2, Git, and ripgrep.
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

### Single-repository fallback

```bash
depbot upgrade --repo tests/fixtures/repos/repo_a --dependency toydep --to 2.0.0 \
  --changelog tests/fixtures/toydep/2.0.0/CHANGELOG.md \
  --conan-home examples/.depbot/conan --approve-major
```

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
- A repository is reported `verified` only if the upgraded install, configure,
  build and tests actually ran and passed.
- Repair attempts are bounded by policy, and an identical patch is never re-applied.

## Status

| Area | Owner | State |
|---|---|---|
| Contracts, CLI, registry, selection, policy, Conan adapter, working copies, engine, fleet runner, PR delivery | A | implemented and unit-tested |
| Release research, code impact analysis | B | in progress |
| Fixtures, migration planning/patching, verification, repair, per-repo reports | C | in progress |

Until B's and C's stages are merged, `depbot run` reports affected repositories as
`BLOCKED: stage '<name>' not integrated yet`. It never reports a fake success.
