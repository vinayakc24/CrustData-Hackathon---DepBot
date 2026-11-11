# DepBot infrastructure

How fleet runs are executed, persisted, recovered, measured and observed. The migration
pipeline itself (research → impact → plan → patch → verify) is described in the README;
this document covers what runs it.

## 1. Execution backends — `depbot.execution`

Every install/configure/build/test command goes through
`verification.command_runner.run_command`, which dispatches to the `ExecutionBackend`
bound for the current job. The pipeline never knows which backend is in use.

| Backend | Use | Isolation |
|---|---|---|
| `local` (default) | development, trusted repositories | allow-listed environment, time limit, whole process tree killed on timeout/cancel. **No filesystem, network or resource isolation.** |
| `docker` / `podman` | untrusted repositories | one disposable container per command (below) |

```bash
depbot run --fleet F --event E --exec-backend docker --exec-image IMAGE@sha256:… \
           [--exec-network-install] [--exec-cpus 2] [--exec-memory-mb 4096] [--exec-pids 512] \
           [--exec-mount-ro PATH] [--exec-mount-rw PATH]
```

Each container runs as a non-root user (the host UID, or 65534 if DepBot runs as root) with
all capabilities dropped, `no-new-privileges`, a read-only root filesystem, a size-capped
`/tmp`, and CPU / memory (no swap) / PID / time limits. The only read-write mount is the
job's own run directory (plus the fleet's isolated Conan home and any `--exec-mount-rw`
path); mounts are placed at identical paths so compile databases and diagnostics stay
valid on the host. The network is `none` unless `--exec-network bridge`, or
`--exec-network-install`, which opens it for the dependency-install step only.
`validate_mount` refuses `/`, the home directory or anything containing it, credential
directories (`~/.ssh`, `~/.aws`, `~/.config`, `~/.docker`, …), system directories and
sockets, so the container-runtime socket and host credentials can never be mounted. The
container sees only the allow-listed variables the adapter asks for, never host `PATH`,
`HOME`, tokens or API keys. After a timeout or cancellation the container is killed by
name, and `rm --force` always runs afterwards.

The image must contain the build toolchain (compiler, CMake, Conan/vcpkg). An unusable
backend (runtime or image missing, unsafe mount) fails the run before anything is recorded.

Still on the host: `git` (clone, diff, apply), `ripgrep`, libclang parsing and manifest
edits. They read untrusted files but never execute repository code.

## 2. Durable orchestration — `depbot.persistence`

State lives in `<state_dir>/depbot.db` (SQLite, WAL). Artifacts stay on the filesystem;
the database stores their path, size and SHA-256.

| Table | Content |
|---|---|
| `release_events` | every processed event; a unique dedup key makes recording idempotent |
| `fleet_runs` | one row per run: status, configuration, cancel flag, summary path |
| `jobs` | one row per repository: status, latest stage, attempt, worker lease, last `DepBotState` checkpoint, outcome, telemetry |
| `transitions`, `stage_runs` | every status transition and every stage attempt with duration |
| `artifacts` | metadata of reports, patches, evidence, telemetry |
| `recovery_log` | requeue / resume / restart / abandon / cancel actions |
| `side_effects` | external effects by idempotency key (GitHub PRs) |

Guarantees:

- **Transactional.** Every mutation is one `BEGIN IMMEDIATE` transaction. A fleet run and
  all of its job rows are created together or not at all.
- **Idempotent events.** The same event (by id or by dependency/from/to/type) is recorded once,
  also when two processes race.
- **No duplicate jobs.** A partial unique index allows one active job per (event, repository);
  a forced re-run reports the repository as blocked instead of running it twice.
- **Leases.** A worker claims a job atomically and heartbeats while it runs. Writes from a
  worker that lost its lease are rejected, and that worker cancels its job.
- **Crash recovery.** `depbot runs resume RUN` re-queues jobs whose lease expired and runs
  everything still queued. A job that outlives three attempts is marked `abandoned` and
  reported as failed.
- **Stage-level resumption.** The engine checkpoints after every transition. A recovered job
  continues from its checkpoint only if the working copy provably matches it: unmodified
  before `PATCHED`, byte-identical to the recorded diff fingerprint from `PATCHED` on.
  Otherwise it restarts from scratch in a new run directory (the old one is kept as
  evidence). The policy gate is re-evaluated on resume and a finished attempt's commit is
  not repeated.
- **Idempotent PRs.** Before creating a PR, DepBot checks the ledger and then GitHub for an
  open PR on the same head branch, and reuses it.
- **Cancellation.** Ctrl-C (or `depbot runs cancel RUN` from another terminal) kills running
  commands, stops jobs at the next stage boundary as `BLOCKED: cancelled`, and never starts
  queued jobs. A cancelled run cannot be resumed.

`events.json` from earlier versions is imported into the database on first use.

## 3. Concurrency — `depbot.fleet.pool`

`depbot run --workers N` runs up to N repository jobs at once on a bounded in-process pool
(maximum 16). The count is capped by the host: `min(N, cpus / --exec-cpus, memory /
--exec-memory-mb)`, never below one. The queue is bounded and `submit` blocks when it is
full. Each job has its own run directory, working copy, state, LLM client and cancel
token; a job that fails or crashes affects only its own outcome. Dependency installs are
serialised across jobs because the package cache is shared; builds and tests run in
parallel. Outcomes are always reported in fleet order.

## 4. Migration recipes — `depbot.migration.recipes`

A `MigrationRecipe` is a deterministic, code-defined transformation with metadata,
provenance, preconditions, a matching strategy, a transform, postconditions and
verification requirements. The planner selects the first registered recipe whose
preconditions hold for a breaking change; the patch generator runs its transform, checks
that only reported lines changed and checks the recipe's postconditions before a diff is
produced. The diff then goes through `patch_validator` and full verification as before.

| Recipe | Handles |
|---|---|
| `header-relocation` | a moved public header: rewrites real `#include` directives only, keeps delimiter style / indentation / trailing comment / line ending, removes the stale directive when the new header is already included |
| `symbol-rename` | one identifier → its single documented replacement, on proven lines |

`recipes/ast.py` is the extension point for Clang AST transformations: subclass
`AstMigrationRecipe`, return `SourceEdit` ranges from `edits_for`; the base class refuses to
run without libclang, confines edits to cited usages and requires tests. No AST recipe
ships yet.

An LLM can propose a `BreakingChange`; it cannot propose an edit. Only registered recipe
classes produce edits, and a recipe that needs tests is blocked under a policy with
`require_tests: false`.

## 5. Benchmarks — `depbot.benchmark`, `benchmarks/`

`benchmarks/registry.yaml` is a versioned registry. Each case pins a repository (content
hash for in-tree fixtures, commit for Git URLs), the dependency and versions, the release
notes, verification requirements and hand-written ground truth: the breaking changes, the
affected usages, the correct outcome and the migrated lines.

```bash
depbot bench list
depbot bench run                 # dev split
depbot bench run --split all     # dev + held-out
depbot bench run --case header-relocation --llm gemini
depbot bench hash PATH           # pin a new fixture
```

The runner copies each pinned repository, prepares an isolated Conan home, and runs one job
through the real engine with the default stages. It writes `results.json` and `report.md`.

| Metric | Definition |
|---|---|
| Breaking-change precision / recall | predicted vs expected (affected symbols, replacement), namespace-insensitive |
| Impact precision / recall | `definite` usages vs expected (file, line, symbol) |
| Outcome accuracy | actual outcome equals the expected (or a listed acceptable) one |
| Patch correctness | for cases whose correct outcome is `verified`: verified, exactly the expected files edited, expected lines present |
| Verification success | upgraded verification passed where it should |
| Safe-abstention accuracy | not verified, for cases whose correct outcome is not `verified` |
| False verified | verified although abstention was correct, or verified with a wrong patch. `bench run` exits 1 if non-zero |
| Duration, LLM calls / tokens / cost | from job telemetry; cost only when the provider reports tokens and `DEPBOT_LLM_USD_PER_MTOK_INPUT` / `_OUTPUT` are set |

A stage that never ran (the job correctly stopped earlier) is not scored. A case that cannot
run (missing tool, drifted fixture) is listed as `not_run` and contributes to no metric.
Held-out cases run only with `--split held_out|all` and are reported in their own column;
do not tune against them. `benchmarks/results/` holds the report of a real run.

## 6. Telemetry and inspection — `depbot.telemetry`

Every job writes `telemetry.json` (also stored with the job in SQLite): fleet-run / job /
run ids, attempt and resume point, repository commit, dependency versions, host toolchain
versions, execution backend and image digest, every stage span with duration and status,
repair attempts, evidence references, patch hashes and the final diff hash, per-phase
build/test results, LLM provider / model / calls / tokens when the provider reports them,
and the final outcome.

```bash
depbot runs list    --fleet F
depbot runs show    RUN --fleet F [--job REPOSITORY_ID] [--json]
depbot runs compare RUN_A RUN_B --fleet F
depbot runs cancel  RUN --fleet F
depbot runs resume  RUN --fleet F
```

With a container backend the recorded toolchain versions are the host's; the image digest
identifies the toolchain that actually built.
