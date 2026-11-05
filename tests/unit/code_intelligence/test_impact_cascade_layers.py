"""Impact cascade layers I2 (libclang) and I3 (compiler probe) inside find_impacted_usages."""

import json
from pathlib import Path

import pytest

from depbot.clang_support import ClangUnavailable, load_cindex
from depbot.code_intelligence.impact_analyzer import find_impacted_usages
from depbot.models import AnalysisMethod, BreakingChange, ChangeType, ImpactLevel, ResolutionLayer

try:
    load_cindex()
    HAVE_CLANG = True
except ClangUnavailable:
    HAVE_CLANG = False
needs_clang = pytest.mark.skipif(not HAVE_CLANG, reason="libclang not available")

HEADER = "#pragma once\nnamespace toydep {\nint old_sum(int a, int b);\n}\n"
SOURCE = """\
#include <toydep/toydep.hpp>
namespace mylib { int old_sum(int a, int b) { return a - b; } }
// old_sum is mentioned in a comment
int direct() { return toydep::old_sum(1, 2); }
int other() { return mylib::old_sum(3, 1); }
#ifdef _WIN32
int windows_only() { return toydep::old_sum(9, 9); }
#endif
"""
CHANGE = BreakingChange(id="toydep-2.0.0-BC1", title="old_sum removed", change_type=ChangeType.REMOVED,
                        affected_symbols=["old_sum"], replacement_symbols=["sum"], migration_advice="use sum",
                        evidence_urls=["file:///notes.md"], confidence=0.95)


@pytest.fixture
def layout(tmp_path):
    dep = tmp_path / "conan" / "toydep" / "include" / "toydep"
    dep.mkdir(parents=True)
    (dep / "toydep.hpp").write_text(HEADER)
    wt = tmp_path / "worktree"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "main.cpp").write_text(SOURCE)
    (wt / "src" / "orphan.cpp").write_text("int f() { return old_sum(1, 2); }\n")  # in no TU
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "compile_commands.baseline.json").write_text(json.dumps([{
        "directory": str(wt / "build"), "file": str(wt / "src" / "main.cpp"),
        "arguments": ["c++", "-std=c++17", "-U_WIN32", "-isystem", str(dep.parent), "-o", "main.o", "-c",
                      str(wt / "src" / "main.cpp")]}]))
    return wt, art


def _by_line(usages, path="src/main.cpp"):
    return {u.line_start: u for u in usages if u.file_path == path}


@needs_clang
def test_ast_layer_removes_text_false_positives(layout):
    wt, art = layout
    usages = find_impacted_usages(worktree=wt, changes=[CHANGE], artifacts_dir=art)
    lines = _by_line(usages)
    assert lines[4].impact == ImpactLevel.DEFINITE and lines[4].resolved_by == ResolutionLayer.CLANG_AST
    assert lines[4].resolved_usr == "c:@N@toydep@F@old_sum#I#I#" and lines[4].analysis_method == AnalysisMethod.CLANG_AST
    other = lines[5]  # text search alone calls `mylib::old_sum(3, 1)` definite
    assert other.impact == ImpactLevel.UNAFFECTED and "mylib" in other.resolution_proof
    assert lines[2].impact == ImpactLevel.UNAFFECTED and "different entity" in lines[2].resolution_proof
    assert lines[3].impact == ImpactLevel.UNAFFECTED and "comment" in lines[3].resolution_proof
    win = lines[7]
    assert win.impact == ImpactLevel.POSSIBLE and win.preprocessor_context == "#ifdef _WIN32"
    orphan = _by_line(usages, "src/orphan.cpp")[1]  # clear call, but no TU compiles this file
    assert orphan.impact == ImpactLevel.POSSIBLE and "not part of any compiled translation unit" in orphan.resolution_proof
    stats = json.loads((art / "impact_layers.json").read_text())
    assert stats["ast"]["confirmed"] == 1 and stats["ast"]["ruled_out"] == 3 and stats["ast"]["residual"] == 1


@needs_clang
def test_r1_usrs_are_used_when_present(layout):
    wt, art = layout
    exact = CHANGE.model_copy(update={"affected_usrs": ["c:@N@toydep@F@old_sum#I#I#"]})
    lines = _by_line(find_impacted_usages(worktree=wt, changes=[exact], artifacts_dir=art))
    assert lines[4].impact == ImpactLevel.DEFINITE and lines[5].impact == ImpactLevel.UNAFFECTED


@needs_clang
def test_probe_diagnostics_confirm_residuals_and_surface_unexplained_breaks(layout):
    wt, art = layout
    (art / "probe").mkdir()
    (art / "probe" / "diagnostics.json").write_text(json.dumps([
        {"file_path": "src/main.cpp", "line": 7, "column": 37, "severity": "error",
         "message": "'old_sum' is not a member of 'toydep'", "option": None, "command_label": "probe.build"},
        {"file_path": "src/main.cpp", "line": 1, "column": 10, "severity": "error",
         "message": "'toydep/legacy.hpp' file not found", "option": None, "command_label": "probe.build"},
        {"file_path": None, "line": 3, "column": 1, "severity": "note", "message": "declared here",
         "option": None, "command_label": "probe.build"},
    ]))
    usages = find_impacted_usages(worktree=wt, changes=[CHANGE], artifacts_dir=art)
    lines = _by_line(usages)
    assert lines[7].impact == ImpactLevel.DEFINITE and lines[7].resolved_by == ResolutionLayer.COMPILER_PROBE
    assert "probe build error" in lines[7].resolution_proof
    unexplained = lines[1]
    assert unexplained.impact == ImpactLevel.UNKNOWN and unexplained.symbol == "<unexplained>"
    assert unexplained.related_change_ids == [] and "not explained by any known breaking change" in unexplained.resolution_proof
    assert lines[5].impact == ImpactLevel.UNAFFECTED  # probe never overrides an AST ruling-out
    stats = json.loads((art / "impact_layers.json").read_text())
    assert stats["probe"] == {"errors": 2, "confirmed": 1, "unexplained_added": 1}


def test_without_compile_database_text_behaviour_is_unchanged(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "a.cpp").write_text("int x() { return mylib::old_sum(1, 2); }\n")
    (usage,) = find_impacted_usages(worktree=wt, changes=[CHANGE], artifacts_dir=tmp_path / "art")
    assert usage.impact == ImpactLevel.DEFINITE and usage.resolved_by == ResolutionLayer.TEXT_PREFILTER
    stats = json.loads((tmp_path / "art" / "impact_layers.json").read_text())
    assert stats["ast"] == "skipped: no baseline compile database"


ALIAS_HEADER = """\
#pragma once
namespace fmt { inline namespace v6 {
template <typename Char> class basic_format_parse_context {};
using format_parse_context = basic_format_parse_context<char>;
using parse_context [[deprecated]] = basic_format_parse_context<char>;
}}
"""
ALIAS_SOURCE = """\
#include <fmt/core.h>
namespace mine { struct parse_context {}; }
struct F {
  void parse(fmt::parse_context& ctx);
  void other(mine::parse_context& ctx);
};
"""


@needs_clang
def test_type_alias_uses_resolve_through_type_refs(tmp_path):
    """fmt 7: `parse_context` (an alias) removed — TYPE_REF cursors are spelled qualified."""
    dep = tmp_path / "conan" / "fmt" / "include" / "fmt"
    dep.mkdir(parents=True)
    (dep / "core.h").write_text(ALIAS_HEADER)
    wt = tmp_path / "worktree"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "main.cpp").write_text(ALIAS_SOURCE)
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "compile_commands.baseline.json").write_text(json.dumps([{
        "directory": str(wt / "build"), "file": str(wt / "src" / "main.cpp"),
        "arguments": ["c++", "-std=c++17", "-isystem", str(dep.parent), "-c", str(wt / "src" / "main.cpp")]}]))
    change = BreakingChange(id="fmt-7.0.0-BC3", title="parse_context removed", change_type=ChangeType.REMOVED,
                            affected_symbols=["parse_context"], replacement_symbols=["format_parse_context"],
                            migration_advice="use format_parse_context", evidence_urls=["file:///c.md"],
                            confidence=0.95)
    lines = _by_line(find_impacted_usages(worktree=wt, changes=[change], artifacts_dir=art))
    assert lines[4].impact == ImpactLevel.DEFINITE and lines[4].resolved_by == ResolutionLayer.CLANG_AST
    assert lines[4].resolved_usr == "c:@N@fmt@N@v6@parse_context"
    assert lines[5].impact == ImpactLevel.UNAFFECTED  # mine::parse_context is a different entity
