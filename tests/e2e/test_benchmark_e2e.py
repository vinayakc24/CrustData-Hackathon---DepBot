"""Benchmark cases through real Conan, CMake and CTest (the new recipe and an abstention)."""

import shutil
from pathlib import Path

import pytest

from depbot.benchmark.models import load_registry
from depbot.benchmark.runner import BenchmarkRunner
from depbot.orchestrator import load_default_stages

REGISTRY = Path(__file__).resolve().parents[2] / "benchmarks" / "registry.yaml"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        any(shutil.which(tool) is None for tool in ("conan", "cmake", "rg")),
        reason="Conan, CMake and ripgrep must be on PATH",
    ),
]


def test_header_relocation_and_behavior_change_cases(tmp_path):
    runner = BenchmarkRunner(load_registry(REGISTRY), REGISTRY, tmp_path / "out", stages_factory=load_default_stages)
    results = runner.run(splits=["dev", "held_out"], case_ids=[
        "header-relocation", "header-relocation-already-included", "behavior-change-no-replacement"])
    by_id = {case.case_id: case for case in results.cases}
    assert all(case.status == "completed" for case in results.cases), [c.reason for c in results.cases]

    moved = by_id["header-relocation"]
    assert moved.actual_outcome == "verified" and moved.patch_correct and moved.verification_passed
    assert moved.breaking_changes.recall == 1.0 and moved.impact.precision == 1.0 and moved.impact.recall == 1.0
    source = (Path(moved.artifacts_dir) / "worktree" / "src" / "main.cpp").read_text()
    assert "#include <geomdep/core.hpp>" in source and "// Area helpers come from geomdep/legacy.hpp" in source

    dup = by_id["header-relocation-already-included"]
    assert dup.patch_correct and (Path(dup.artifacts_dir) / "worktree" / "src" / "main.cpp").read_text().count(
        "#include") == 1

    behavior = by_id["behavior-change-no-replacement"]
    assert behavior.actual_outcome in ("failed", "blocked") and behavior.abstained and not behavior.false_verified
    assert results.aggregate["overall"].false_verified == 0
