"""Aggregate Markdown report for a benchmark run."""

from __future__ import annotations

from depbot.benchmark.models import Aggregate, BenchmarkResults


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _pair(counts, ran: bool) -> str:
    return f"{_pct(counts.precision)} / {_pct(counts.recall)}" if ran else "not reached"


def _num(value) -> str:
    return "n/a" if value is None else str(value)


_ROWS = [
    ("Cases (completed / not run / error)", lambda a: f"{a.completed} / {a.not_run} / {a.errors}"),
    ("Breaking-change precision", lambda a: _pct(a.breaking_change_precision)),
    ("Breaking-change recall", lambda a: _pct(a.breaking_change_recall)),
    ("Impact-analysis precision", lambda a: _pct(a.impact_precision)),
    ("Impact-analysis recall", lambda a: _pct(a.impact_recall)),
    ("Outcome accuracy", lambda a: _pct(a.outcome_accuracy)),
    ("Patch correctness", lambda a: _pct(a.patch_correctness)),
    ("Verification success", lambda a: _pct(a.verification_success)),
    ("Safe-abstention accuracy", lambda a: _pct(a.safe_abstention_accuracy)),
    ("False verified outcomes", lambda a: str(a.false_verified)),
    ("Execution duration (s)", lambda a: f"{a.duration_seconds:.1f}"),
    ("LLM calls", lambda a: str(a.llm_calls)),
    ("LLM tokens (in / out)", lambda a: f"{_num(a.llm_input_tokens)} / {_num(a.llm_output_tokens)}"),
    ("LLM cost (USD)", lambda a: _num(a.llm_cost_usd)),
]


def render_markdown(results: BenchmarkResults) -> str:
    splits = [name for name in ("overall", "dev", "held_out") if name in results.aggregate]
    out = [
        f"# DepBot migration benchmark — {results.suite} {results.suite_version}",
        "",
        f"- Generated: {results.generated_at}",
        f"- DepBot: {results.depbot_version}" + (f" ({results.depbot_commit[:12]})" if results.depbot_commit else ""),
        f"- LLM: {results.llm}",
        f"- Execution backend: {results.execution.get('backend')}"
        + (f" ({results.execution.get('image_digest')})" if results.execution.get("image_digest") else ""),
        f"- Splits run: {', '.join(results.splits_run)}",
        "- Toolchain: " + "; ".join(f"{k} {v}" for k, v in results.toolchain.items() if v and k != "platform"),
        "",
        "Metrics count only completed cases. A case that could not run is listed with its",
        "reason and contributes to no number.",
        "",
        "## Aggregate",
        "",
        "| Metric | " + " | ".join(splits) + " |",
        "|---|" + "---|" * len(splits),
    ]
    for label, render in _ROWS:
        cells = [render(results.aggregate.get(s, Aggregate())) for s in splits]
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out += ["", "## Cases", "",
            "| Case | Split | Expected | Actual | Changes P/R | Impact P/R | Patch | False verified | Seconds |",
            "|---|---|---|---|---|---|---|---|---|"]
    for case in results.cases:
        if case.status != "completed":
            out.append(f"| {case.case_id} | {case.split} | {case.expected_outcome} | "
                       f"**{case.status.upper()}** | – | – | – | – | – |")
            continue
        patch = {True: "correct", False: "WRONG", None: "n/a"}[case.patch_correct]
        mark = "" if case.outcome_match else " ✗"
        out.append(
            f"| {case.case_id} | {case.split} | {case.expected_outcome} | {case.actual_outcome}{mark} | "
            f"{_pair(case.breaking_changes, case.research_ran)} | "
            f"{_pair(case.impact, case.impact_ran)} | {patch} | "
            f"{'YES' if case.false_verified else 'no'} | {_num(case.duration_seconds)} |"
        )
    notes = [c for c in results.cases
             if c.status != "completed" or not c.outcome_match or c.patch_problems or c.change_type_mismatches]
    if notes:
        out += ["", "## Notes", ""]
        for case in notes:
            if case.status != "completed":
                out.append(f"- **{case.case_id}** {case.status.replace('_', ' ')}: {case.reason}")
                continue
            if not case.outcome_match:
                out.append(f"- **{case.case_id}** expected `{case.expected_outcome}`, got `{case.actual_outcome}`: "
                           f"{case.outcome_reason or 'no reason recorded'}")
            for problem in case.patch_problems:
                out.append(f"- **{case.case_id}** patch: {problem}")
            for mismatch in case.change_type_mismatches:
                out.append(f"- **{case.case_id}** change type: {mismatch}")
    return "\n".join(out) + "\n"
