# Spike: libclang USR resolution (impact cascade layer I2)

Run on 2026-11-06 with clang 18.1.3 (system libclang) and the `clang==18.1.8` Python bindings.

```bash
pip install "clang==18.1.8"
python docs/spikes/clang_usr_resolution/classify.py \
  --dep-include docs/spikes/clang_usr_resolution/dep/include \
  --source docs/spikes/clang_usr_resolution/proj/src/main.cpp --symbol old_sum
```

`ripgrep -w old_sum` reports **11** matches in `proj/src/main.cpp`. Classification without an LLM:

| Line | Code | Verdict | Proof |
|---:|---|---|---|
| 2 | `namespace mylib { int old_sum(...)` | RULED_OUT | declares `c:@N@mylib@F@old_sum#I#I#` |
| 3 | `// old_sum ...` | RULED_OUT | comment token |
| 4 | `"old_sum"` | RULED_OUT | string literal token |
| 5 | `toydep::old_sum(1, 2)` | CONFIRMED | references `c:@N@toydep@F@old_sum#I#I#` |
| 6 | `mylib::old_sum(3, 1)` | RULED_OUT | references the mylib USR |
| 7 | `using toydep::old_sum;` | CONFIRMED | using-declaration of the toydep USR |
| 8 | `old_sum(4, 5)` (via using) | CONFIRMED | toydep USR |
| 9 | `#define ADD(a, b) toydep::old_sum(a, b)` | CONFIRMED | expansion on line 10 resolves to toydep → **edit the macro body** |
| 11 | template body `toydep::old_sum(a, a)` | CONFIRMED | toydep USR |
| 13 | `&toydep::old_sum` | CONFIRMED | toydep USR |
| 15 | inside `#ifdef _WIN32` | RESIDUAL | no AST node in this configuration |

The result is 6 confirmed, 4 ruled out with proof, and **1 residual** (the only case left for later layers).
Re-parsing with `-D_WIN32` turns line 15 into CONFIRMED, so extra configurations shrink the residue.

## Notes for the implementation (Member B, `code_intelligence/clang_analyzer.py`)

- Target set = USRs of declarations of the changed symbol whose file lies under the
  dependency's include directory. This is what removes same-name false positives.
- `OVERLOADED_DECL_REF` (using-declarations, unresolved overload sets) needs
  `clang_getNumOverloadedDecls` / `clang_getOverloadedDecl`. The clang 18 Python bindings
  don't wrap them, so register them via `ci.conf.lib` (see `classify.py`).
- Expression cursors produced by macros are reported at the expansion site. Map them back to the
  `MACRO_DEFINITION` line, because that's where the patch must go.
- Token kinds (`COMMENT`, `LITERAL`) rule out comments and strings without any heuristics.
- An identifier with no cursor at all means an inactive preprocessor branch or uncompiled code →
  RESIDUAL (record `#if` context for the LLM/human layer).
- In the product, use the flags from `compile_commands.json` (recorded by the engine in
  `state.repo_facts["compile_commands"]`) instead of the hand-written `-I`/`-std` above.
