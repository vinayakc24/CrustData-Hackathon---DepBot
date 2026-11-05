"""Live LLM check for P0 item 4: R3 (release interpretation) and I4 (residual impact).

Runs DepBot's real research and impact stages on tiny inputs that the deterministic
layers deliberately cannot settle, once without and once with a live provider, and
prints what the LLM added. Needs no Conan, CMake or libclang.

    GEMINI_API_KEY=... python scripts/llm_live_check.py --llm gemini
    LLM_API_KEY=...    python scripts/llm_live_check.py --llm anthropic
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from depbot.code_intelligence.impact_analyzer import find_impacted_usages
from depbot.llm.base import LLMClient
from depbot.llm.runtime import use_llm_client
from depbot.models import BreakingChange, ChangeType, Dependency, ReleaseEvent
from depbot.research.breaking_change_analyzer import research_release

# Prose R2's rules do not parse ("is gone", "switch to"), but "Breaking" flags it as a leftover.
RELEASE_NOTES = """\
# toydep 2.0.0

Breaking: the legacy adder `toydep::old_sum(int, int)` is gone in this release; callers
should switch to `toydep::sum(int, int)`, which keeps the same addition behavior.

Performance of `toydep::sum` improved by 10%.
"""

# Text matches that are not clear calls, so I1 leaves them `possible` for I4.
SOURCE = """\
#include <toydep/toydep.hpp>
// old_sum used to live here before 2.0; see the release notes
using adder = int (*)(int, int);
adder pick() { return &toydep::old_sum; }
#define ADD toydep::old_sum
"""

CHANGE = BreakingChange(
    id="toydep-2.0.0-BC1", title="old_sum removed", change_type=ChangeType.REMOVED,
    affected_symbols=["old_sum"], replacement_symbols=["sum"], migration_advice="use sum",
    evidence_urls=["file:///release-notes.md"], evidence_excerpt="removes `toydep::old_sum(int, int)`",
    confidence=0.95)


def _client(kind: str, model: str | None) -> LLMClient:
    if kind == "gemini":
        from depbot.llm.gemini import GeminiLLMClient
        return GeminiLLMClient.from_env(model=model)
    from depbot.llm.provider import AnthropicLLMClient
    return AnthropicLLMClient.from_env(model=model)


def _research(work: Path, client: LLMClient | None) -> list[BreakingChange]:
    notes = work / "release-notes.md"
    notes.write_text(RELEASE_NOTES, encoding="utf-8")
    event = ReleaseEvent(event_id="toydep-v2-prose", dependency_name="toydep", upstream_repo="local/toydep",
                         previous_version="1.0.0", target_version="2.0.0", changelog_path=str(notes))
    dep = Dependency(name="toydep", current_version="1.0.0", target_version="2.0.0",
                     package_manager="conan", manifest_path="conanfile.txt")
    with use_llm_client(client):
        return research_release(event=event, dependency=dep, artifacts_dir=work / ("r3" if client else "r2"))


def _impact(work: Path, client: LLMClient | None):
    wt = work / "wt"
    (wt / "src").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "main.cpp").write_text(SOURCE, encoding="utf-8")
    with use_llm_client(client):
        return find_impacted_usages(worktree=wt, changes=[CHANGE], artifacts_dir=work / ("i4" if client else "i1"))


def run(client: LLMClient) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        before, after = _research(work, None), _research(work, client)
        usages_before, usages_after = _impact(work, None), _impact(work, client)
    def row(c): return {"id": c.id, "type": c.change_type.value, "source": c.source.value,
                        "symbols": c.affected_symbols, "replacements": c.replacement_symbols,
                        "confidence": c.confidence, "excerpt": c.evidence_excerpt}
    report = {
        "r3": {"without_llm": [row(c) for c in before], "with_llm": [row(c) for c in after]},
        "i4": [{"line": a.line_start, "snippet": a.snippet, "before": b.impact.value, "after": a.impact.value,
                "resolved_by": a.resolved_by.value if a.resolved_by else None, "reason": a.resolution_proof}
               for b, a in zip(usages_before, usages_after)],
    }
    llm_changes = [c for c in after if c.source.value == "llm"]
    report["checks"] = {
        "r3_llm_called": bool(getattr(client, "calls", None)) and "breaking_changes" in client.calls,
        "r3_found_old_sum_to_sum": any("old_sum" in " ".join(c.affected_symbols) and
                                       any("sum" in r for r in c.replacement_symbols) for c in llm_changes),
        "r3_excerpts_verbatim": all(c.evidence_excerpt in RELEASE_NOTES for c in llm_changes),
        "r3_did_not_flag_performance_note": not any("performance" in (c.evidence_excerpt or "").lower()
                                                    for c in llm_changes),
        "i4_llm_called": "impact_classification" in getattr(client, "calls", []),
        "i4_never_definite": all(u["after"] != "definite" or u["before"] == "definite" for u in report["i4"]),
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", choices=["gemini", "anthropic"], required=True)
    ap.add_argument("--llm-model")
    args = ap.parse_args()
    report = run(_client(args.llm, args.llm_model))
    print(json.dumps(report, indent=2))
    failed = [k for k, ok in report["checks"].items() if not ok]
    print("\nALL CHECKS PASSED" if not failed else f"\nFAILED CHECKS: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
