"""R1 header API diff (libclang). Skipped when libclang is unavailable."""

import json
import textwrap

import pytest

import depbot.research.header_diff as hd
from depbot.clang_support import ClangUnavailable, load_cindex
from depbot.models import BreakingChange, ChangeSource, ChangeType
from depbot.repository.compile_db import CompileCommand

try:
    load_cindex()
    HAVE_CLANG = True
except ClangUnavailable:
    HAVE_CLANG = False
needs_clang = pytest.mark.skipif(not HAVE_CLANG, reason="libclang not available")

OLD = """\
#pragma once
#define TOYDEP_VERSION 1
#define TOYDEP_ADD(a, b) toydep::old_sum(a, b)
namespace toydep {
int old_sum(int a, int b);
int scale(int a, int k);
long area(int w, int h);
int keep(int a);
void legacy();
namespace detail { int helper(); }
struct Point { int x; int y; private: int secret; };
enum class Mode { Fast, Safe };
}
"""
NEW = """\
#pragma once
#define TOYDEP_VERSION 2
#define TOYDEP_ADD(a, b) toydep::sum(a, b)
namespace toydep {
int sum(int a, int b);
int scale(int a, double k);
long long area(int w, int h);
int keep(int a);
[[deprecated("use modern()")]] void legacy();
void modern();
namespace detail { int helper2(); }
struct Point { int x; int y; private: int secret2; };
enum class Mode { Fast };
}
"""


def _dirs(tmp_path, old=OLD, new=NEW, extra_unchanged=True):
    for side, text in (("old", old), ("new", new)):
        (tmp_path / side / "toydep").mkdir(parents=True)
        (tmp_path / side / "toydep" / "toydep.hpp").write_text(text)
        if extra_unchanged:
            (tmp_path / side / "toydep" / "util.hpp").write_text("#pragma once\nnamespace toydep { int same(); }\n")
    return [tmp_path / "old"], [tmp_path / "new"]


@needs_clang
def test_detects_each_kind_of_api_change(tmp_path):
    old, new = _dirs(tmp_path)
    r = hd.diff_api(dependency="toydep", to_version="2.0.0", old_dirs=old, new_dirs=new)
    assert r.available and r.complete and r.changed_headers == ["toydep/toydep.hpp"]  # util.hpp not parsed
    got = {(c.change_type, c.title) for c in r.changes}
    assert got == {
        (ChangeType.REMOVED, "toydep::old_sum was removed"),
        (ChangeType.SIGNATURE, "toydep::scale signature changed"),
        (ChangeType.SIGNATURE, "toydep::area signature changed"),
        (ChangeType.REMOVED, "toydep::Mode::Safe was removed"),
        (ChangeType.DEPRECATED, "toydep::legacy is deprecated"),
        (ChangeType.BEHAVIOR, "macro TOYDEP_ADD changed"),
    }
    removed = next(c for c in r.changes if c.title == "toydep::old_sum was removed")
    assert removed.source == ChangeSource.API_DIFF and removed.affected_symbols == ["old_sum"]
    assert removed.affected_usrs == ["c:@N@toydep@F@old_sum#I#I#"]
    assert removed.replacement_symbols == []  # never invented by R1
    assert "toydep::sum (candidate replacement" in removed.migration_advice
    assert removed.evidence_excerpt == "int old_sum(int a, int b);" and removed.evidence_urls[0].startswith("file://")
    titles = " ".join(c.title for c in r.changes)
    for absent in ("keep", "detail", "helper", "secret", "modern", "TOYDEP_VERSION", "same"):
        assert absent not in titles


@needs_clang
def test_parse_errors_in_new_version_withhold_removals(tmp_path):
    old, new = _dirs(tmp_path, new='#pragma once\n#include "missing/dep.hpp"\nnamespace toydep { int sum(int, int); }\n')
    r = hd.diff_api(dependency="toydep", to_version="2.0.0", old_dirs=old, new_dirs=new)
    assert not r.complete and r.new_parse_errors and "withheld" in r.reason
    assert not [c for c in r.changes if c.change_type in (ChangeType.REMOVED, ChangeType.SIGNATURE)]


@needs_clang
def test_identical_headers_mean_no_changes(tmp_path):
    old, new = _dirs(tmp_path, new=OLD)
    r = hd.diff_api(dependency="toydep", to_version="2.0.0", old_dirs=old, new_dirs=new)
    assert r.changes == [] and r.changed_headers == [] and "no dependency header changed" in r.reason


def test_unavailable_libclang_is_reported(tmp_path, monkeypatch):
    def boom():
        raise ClangUnavailable("no libclang here")
    monkeypatch.setattr(hd, "load_cindex", boom)
    r = hd.diff_api(dependency="d", to_version="2", old_dirs=[tmp_path], new_dirs=[tmp_path])
    assert not r.available and r.reason == "no libclang here" and r.changes == []


def test_dependency_include_dirs_excludes_project_sources(tmp_path):
    wt = tmp_path / "wt"
    conan_include = (tmp_path / "conan/p/toydep1/include").resolve()
    entry = CompileCommand(file=str(wt / "src/a.cpp"), directory=str(wt / "build"), arguments=[
        "c++", f"-I{wt}/src", f"-I{wt}/include", "-isystem", str(conan_include),
        f"-I{wt}/vcpkg_installed/x64-linux/include", f"-I{wt}/build/dependency/toydep/include", "-c", "a.cpp"])
    got = {str(p) for p in hd.dependency_include_dirs([entry], wt)}
    assert got == {str(conan_include), str((wt / "vcpkg_installed/x64-linux/include").resolve()),
                   str((wt / "build/dependency/toydep/include").resolve())}


def test_project_flags():
    e = CompileCommand(file="a.cpp", directory="/", arguments=["c++", "-std=gnu++20", "-DFOO=1", "-D", "BAR", "-O2"])
    assert hd.project_flags([e]) == ["-std=gnu++20", "-DFOO=1", "-D", "BAR"]
    assert hd.project_flags([]) == ["-std=c++17"]


def _job_artifacts(tmp_path, old_dir, new_dir, shared):
    art = tmp_path / "art"
    (art / "worktree" / "src").mkdir(parents=True)
    (art / "probe" / "worktree" / "src").mkdir(parents=True)
    def db(wt, dep):
        return [{"directory": str(wt / "build"), "file": str(wt / "src/main.cpp"),
                 "arguments": ["c++", "-std=c++17", f"-I{wt}/src", "-isystem", str(dep), "-isystem", str(shared),
                               "-c", str(wt / "src/main.cpp")]}]
    (art / "compile_commands.baseline.json").write_text(json.dumps(db(art / "worktree", old_dir)))
    (art / "probe" / "compile_commands.json").write_text(json.dumps(db(art / "probe" / "worktree", new_dir)))
    return art


@needs_clang
def test_job_entry_point_isolates_the_upgraded_dependency(tmp_path):
    old, new = _dirs(tmp_path)
    shared = tmp_path / "zlib" / "include"
    shared.mkdir(parents=True)
    (shared / "zlib.h").write_text("#pragma once\nint deflate_all();\n")
    art = _job_artifacts(tmp_path, old[0], new[0], shared)
    r = hd.diff_dependency_api_for_job(artifacts_dir=art, dependency="toydep", to_version="2.0.0")
    assert r.available and r.old_include_dirs == [str(old[0].resolve())] and r.new_include_dirs == [str(new[0].resolve())]
    assert any(c.title == "toydep::old_sum was removed" for c in r.changes)


def test_job_entry_point_without_databases(tmp_path):
    r = hd.diff_dependency_api_for_job(artifacts_dir=tmp_path, dependency="d", to_version="2")
    assert not r.available and "compile_commands.baseline.json" in r.reason and "probe/compile_commands.json" in r.reason


@needs_clang
def test_changelog_replacement_is_confirmed_only_if_it_exists(tmp_path):
    old, new = _dirs(tmp_path)
    r = hd.diff_api(dependency="toydep", to_version="2.0.0", old_dirs=old, new_dirs=new)
    removed = [c for c in r.changes if c.title == "toydep::old_sum was removed"]

    def rule(replacement):
        return BreakingChange(id="toydep-2.0.0-BC1", title="old_sum removed", change_type="removed",
                              affected_symbols=["old_sum"], replacement_symbols=[replacement],
                              migration_advice=f"Use {replacement}.", evidence_urls=["file:///notes.md"],
                              confidence=0.95, source="changelog_rule")
    ok = hd.confirm_replacements(removed, [rule("sum")], r)[0]
    assert ok.replacement_symbols == ["sum"] and ok.migration_advice == "Use sum."
    assert "file:///notes.md" in ok.evidence_urls
    bogus = hd.confirm_replacements(removed, [rule("totally_new_sum")], r)[0]
    assert bogus.replacement_symbols == []  # replacement not in the new headers → not attached
