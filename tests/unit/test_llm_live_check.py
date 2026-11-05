"""The live LLM check script, driven by a stub client (no network)."""

import importlib.util
from pathlib import Path

from depbot.llm.base import LLMClient

_spec = importlib.util.spec_from_file_location(
    "llm_live_check", Path(__file__).parents[2] / "scripts" / "llm_live_check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


class StubClient(LLMClient):
    def __init__(self):
        self.calls = []

    def complete_structured(self, *, task, system, user, schema):
        self.calls.append(task)
        if task == "breaking_changes":
            excerpt = ("the legacy adder `toydep::old_sum(int, int)` is gone in this release; callers\n"
                       "should switch to `toydep::sum(int, int)`")
            return schema.model_validate({"changes": [{
                "title": "old_sum removed", "change_type": "removed", "affected_symbols": ["toydep::old_sum"],
                "replacement_symbols": ["toydep::sum"], "migration_advice": "switch to sum",
                "evidence_excerpt": excerpt, "confidence": 0.8}]})
        return schema.model_validate({"classifications": [
            {"id": i, "classification": "unaffected" if i == 0 else "possible", "reason": "stub"}
            for i in range(3)]})


def test_live_check_report_with_stub_client():
    report = check.run(StubClient())
    assert report["r3"]["without_llm"] and all(c["source"] != "llm" for c in report["r3"]["without_llm"])
    assert [c for c in report["r3"]["with_llm"] if c["source"] == "llm"]
    assert len(report["i4"]) == 3
    assert all(report["checks"].values()), report["checks"]


def test_replacement_missing_from_evidence_is_dropped():
    class Unsupported(StubClient):
        def complete_structured(self, *, task, system, user, schema):
            if task != "breaking_changes":
                return super().complete_structured(task=task, system=system, user=user, schema=schema)
            self.calls.append(task)
            return schema.model_validate({"changes": [{
                "change_type": "removed", "affected_symbols": ["toydep::old_sum"],
                "replacement_symbols": ["toydep::sum"],
                "evidence_excerpt": "the legacy adder `toydep::old_sum(int, int)` is gone in this release"}]})
    assert check.run(Unsupported())["checks"]["r3_found_old_sum_to_sum"] is False


def test_partial_quote_still_explains_the_unknown_paragraph():
    # Default stub quotes only part of the "Breaking: ..." paragraph.
    with_llm = check.run(StubClient())["r3"]["with_llm"]
    assert not [c for c in with_llm if c["type"] == "unknown"], with_llm
