"""plan_confidence_gate judges only the changes this repository's migration relies on."""

from depbot.models import (AnalysisMethod, BreakingChange, ChangeType, CodeUsage, ImpactLevel, PlannedChange,
                           UpgradePolicy)
from depbot.policy.engine import plan_confidence_gate


def _change(cid, confidence, symbols=("x",)):
    return BreakingChange(id=cid, title=cid, change_type=ChangeType.REMOVED if symbols else ChangeType.UNKNOWN,
                          affected_symbols=list(symbols), migration_advice="m", evidence_urls=["file:///n.md"],
                          confidence=confidence)


def _usage(cid, impact=ImpactLevel.DEFINITE):
    return CodeUsage(symbol="parse_context", file_path="src/main.cpp", line_start=14, line_end=14, snippet="s",
                     analysis_method=AnalysisMethod.RIPGREP, impact=impact, related_change_ids=[cid], confidence=0.9)


# Shaped like the real fmt 6.2.1 -> 7.0.0 run: one relied-on rename, unrelated low-confidence records.
CHANGES = [_change("fmt-7.0.0-BC3", 0.95), _change("fmt-7.0.0-API1", 0.60, ["FMT_STRING"]),
           _change("fmt-7.0.0-BC1", 0.20, [])]
PLAN = [PlannedChange(file_path="src/main.cpp", reason="r", related_change_ids=["fmt-7.0.0-BC3"],
                      expected_change="parse_context -> format_parse_context", confidence=0.95)]


def test_unused_low_confidence_changes_do_not_block_but_are_recorded():
    decision = plan_confidence_gate(UpgradePolicy(), CHANGES, PLAN, [_usage("fmt-7.0.0-BC3")])
    assert decision.allowed
    assert any("not gating" in r and "fmt-7.0.0-API1" in r and "fmt-7.0.0-BC1" in r for r in decision.reasons)


def test_low_confidence_change_cited_by_a_usage_still_blocks():
    for impact in (ImpactLevel.DEFINITE, ImpactLevel.POSSIBLE):
        decision = plan_confidence_gate(UpgradePolicy(), CHANGES, PLAN, [_usage("fmt-7.0.0-API1", impact)])
        assert not decision.allowed and "fmt-7.0.0-API1" in "; ".join(decision.reasons)


def test_ruled_out_usage_does_not_make_a_change_relied_on():
    decision = plan_confidence_gate(UpgradePolicy(), CHANGES, PLAN, [_usage("fmt-7.0.0-API1", ImpactLevel.UNAFFECTED)])
    assert decision.allowed


def test_low_confidence_planned_edit_blocks_and_strict_mode_checks_everything():
    weak = [PLAN[0].model_copy(update={"confidence": 0.5})]
    assert not plan_confidence_gate(UpgradePolicy(), CHANGES, weak, []).allowed
    assert not plan_confidence_gate(UpgradePolicy(), CHANGES, PLAN).allowed  # usages=None: all changes
