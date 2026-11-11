# DepBot — Claude Code entry point

Before making changes, read:

1. `docs/instructions.md` — mandatory coding, safety, collaboration, and efficiency rules.
2. `docs/plan.md` — product scope, architecture, implementation stages, tests, and team schedule.
3. `docs/CONTRACTS.md` — shared models, stage signatures and the contract change log.
4. `docs/INFRASTRUCTURE.md` — execution backends, durable state and recovery, worker pool, recipes, benchmarks.

`README.md` describes the shipped system, the demos, every test suite, the trials run and the known limits.

Do not modify shared contracts (`models.py`, `state.py`, `orchestrator.py`, `cli.py`) unless you are the
integration owner or have explicit approval. Add tests with each module. Never claim a build, test, or
verification passed unless it was actually run.
