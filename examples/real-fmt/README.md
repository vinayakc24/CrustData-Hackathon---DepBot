# Demo B — real upstream upgrade: {fmt} 6.2.1 → 7.0.0

`app/` is a small CMake + Conan service with a custom `fmt::formatter<Point>` whose
`parse` takes `fmt::parse_context&`. fmt 7.0.0 removed that deprecated alias; the
upstream changelog says ``- `*parse_context` - replaced by `*format_parse_context` ``.
Against 7.0.0 the unmodified service no longer compiles.

## Run it

```bash
pip install -e .                      # DepBot, from the repo root
examples/real-fmt/prepare.sh          # clone fmt tags, build both into .depbot/conan (offline)
depbot run --fleet examples/real-fmt/fleet.yaml \
           --event examples/real-fmt/events/fmt-7.0.0.yaml --approve-major
```

## What to expect

| Stage | Evidence |
|---|---|
| Baseline | builds and tests against fmt 6.2.1 (deprecation warning only) |
| Probe | the same code against fmt 7.0.0 fails: `'fmt::parse_context' has not been declared` (GCC) |
| Research | R1 header diff of the two installed fmt trees, R2 changelog rule: `parse_context` → `format_parse_context` (source `changelog_rule`, excerpt quoted) |
| Impact | I1 ripgrep finds `src/main.cpp:14`; I2 libclang resolves it to `c:@N@fmt@N@v6@parse_context` (definite) |
| Patch | `fmt::parse_context&` → `fmt::format_parse_context&` |
| Verify | rebuild and `ctest` against fmt 7.0.0 → **VERIFIED** |

Artifacts land in `examples/real-fmt/.depbot/runs/<run_id>/` (`api_diff.json`,
`impact_layers.json`, the patch, build logs and `pr/pr-body.md`).

No LLM is needed. With `LLM_API_KEY` or `GEMINI_API_KEY` set, R3 and I4 run on any residuals
only, and their output is recorded separately.
