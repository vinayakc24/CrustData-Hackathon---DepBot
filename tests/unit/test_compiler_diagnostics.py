"""Structured diagnostics parsing (GCC/Clang, MSVC, CMake, GNU ld)."""

from depbot.models import CommandResult
from depbot.verification.diagnostics import diagnostics_for, parse_diagnostics

GCC = """\
[ 50%] Building CXX object CMakeFiles/app.dir/src/main.cpp.o
{wt}/src/main.cpp: In function 'int main()':
{wt}/src/main.cpp:4:20: error: 'old_sum' is not a member of 'toydep'; did you mean 'sum'?
    4 |     return toydep::old_sum(2, 3) == 5 ? 0 : 1;
      |                    ^~~~~~~
/conan/p/b/toyde1/p/include/toydep/toydep.hpp:3:12: note: 'toydep::sum' declared here
\x1b[01m\x1b[K{wt}/src/util.cpp:9:5:\x1b[m\x1b[K warning: unused variable 'x' [-Wunused-variable]
src/rel.cpp:2:1: error: expected ';' before '}' token
{wt}/src/main.cpp:4:20: error: 'old_sum' is not a member of 'toydep'; did you mean 'sum'?
"""


def test_gcc_clang_errors_warnings_notes(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    ds = parse_diagnostics(GCC.replace("{wt}", str(wt)), worktree=wt, command_label="probe.build")
    got = [(d.file_path, d.line, d.column, d.severity, d.option) for d in ds]
    assert got == [
        ("src/main.cpp", 4, 20, "error", None),
        (None, 3, 12, "note", None),  # dependency header: outside the worktree
        ("src/util.cpp", 9, 5, "warning", "-Wunused-variable"),
        ("src/rel.cpp", 2, 1, "error", None),  # relative path resolved against the worktree
    ]  # the duplicate error line is collapsed
    assert ds[0].message.startswith("'old_sum' is not a member of 'toydep'") and ds[0].command_label == "probe.build"


def test_msvc_cmake_and_linker_formats(tmp_path):
    wt = tmp_path / "wt"
    text = (
        f"{wt}\\src\\main.cpp(4,20): error C2039: 'old_sum': is not a member of 'toydep'\n"
        "CMake Error at CMakeLists.txt:5 (find_package):\n"
        "  By not providing \"Findtoydep.cmake\" in CMAKE_MODULE_PATH this project has\n"
        "/usr/bin/ld: CMakeFiles/app.dir/src/main.cpp.o: in function `main':\n"
        "/usr/bin/ld: main.cpp:(.text+0x13): undefined reference to `old_sum(int, int)'\n"
        "collect2: error: ld returned 1 exit status\n"
    )
    ds = parse_diagnostics(text, worktree=wt, command_label="upgraded.build")
    msvc = ds[0]
    assert (msvc.severity, msvc.line, msvc.column) == ("error", 4, 20)
    assert msvc.message == "C2039: 'old_sum': is not a member of 'toydep'"
    cmake = next(d for d in ds if d.message.startswith("(find_package)"))
    assert cmake.file_path == "CMakeLists.txt" and cmake.line == 5 and "Findtoydep.cmake" in cmake.message
    link = next(d for d in ds if "undefined reference" in d.message)
    assert link.file_path is None and link.severity == "error" and "old_sum(int, int)" in link.message


def test_full_logs_are_preferred_over_tails(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    log = tmp_path / "build.stderr.log"
    log.write_text("noise\n" * 5000 + f"{wt}/src/main.cpp:4:20: error: 'old_sum' was not declared in this scope\n")
    result = CommandResult(command_label="probe.build", args=["cmake"], cwd=str(wt), exit_code=2,
                           duration_seconds=1, stderr_path=str(log), stderr_tail="(truncated)")
    (d,) = diagnostics_for([result, None], worktree=wt)
    assert d.file_path == "src/main.cpp" and d.message == "'old_sum' was not declared in this scope"
    missing = result.model_copy(update={"stderr_path": str(tmp_path / "gone.log"),
                                        "stderr_tail": f"{wt}/src/a.cpp:1:1: error: boom"})
    assert [x.message for x in diagnostics_for([missing], worktree=wt)] == ["boom"]
