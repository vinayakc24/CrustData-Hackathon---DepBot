# Repository-shape corpus

One toydep 1.0.0 → 2.0.0 event across eleven repos that differ in manifest, layout and
code shape. `expected.json` says what a correct DepBot does with each, including the
cases where the right answer is a safe, explained refusal.

```bash
depbot demo setup --fleet examples/shapes/fleet.yaml \
  --recipe tests/fixtures/toydep/v1 --recipe tests/fixtures/toydep/v2 --recipe examples/shapes/recipes/otherdep
depbot run --fleet examples/shapes/fleet.yaml --event examples/events/toydep-v2.yaml --approve-major
```

| Repo | Shape | Correct result |
|---|---|---|
| s01 | several `[requires]`, comments | VERIFIED; only the toydep line changes |
| s02 | `conan.lock` | VERIFIED; lock re-resolved for toydep only, other pins kept, lock in the change |
| s03 | version range `toydep/[>=1.0 <2.0]` | BLOCKED: range cannot be updated safely |
| s04 | `conanfile.py` | BLOCKED: executable recipe |
| s05 | usages in `include/` and `tests/` | VERIFIED; both migrated |
| s06 | builds, no CTest tests | BLOCKED: policy requires tests |
| s07 | baseline already broken | BLOCKED before any edit; upgrade not blamed |
| s08 | CRLF line endings | VERIFIED; CRLF preserved byte for byte |
| s09 | includes toydep, no API use | UNAFFECTED: manifest bumped and verified, no source edits |
| s10 | old API only in a comment and a string | UNAFFECTED: comment and string untouched |
| s11 | `using toydep::old_sum;` | VERIFIED |

`tests/e2e/test_repo_shapes.py` runs the corpus with real Conan/CMake/CTest and asserts every row
(it re-creates s02's lock against the local cache).
