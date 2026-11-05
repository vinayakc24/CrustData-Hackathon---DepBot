from __future__ import annotations

import json
import io
import subprocess
from pathlib import Path

import pytest

from depbot.code_intelligence import ripgrep_indexer as indexer


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode if self.returncode is not None else None

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "src" / "main.cpp").write_text("int old_sum() { return 1; }\nold_sum();\n", encoding="utf-8")
    (tmp_path / "include" / "api.h").write_text("int old_sum();\n", encoding="utf-8")
    (tmp_path / "include" / "api.hpp").write_text("namespace lib { void old_sum(); }\n", encoding="utf-8")
    return tmp_path


def test_finds_cpp_headers_paths_lines_and_snippets(source_tree: Path) -> None:
    found = indexer.search_symbols(source_tree, ["old_sum"])
    assert [(m.file_path, m.line_number, m.snippet) for m in found] == [
        ("include/api.h", 1, "int old_sum();"),
        ("include/api.hpp", 1, "namespace lib { void old_sum(); }"),
        ("src/main.cpp", 1, "int old_sum() { return 1; }"),
        ("src/main.cpp", 2, "old_sum();"),
    ]
    assert all(m.symbol == "old_sum" for m in found)


def test_multiple_symbols_deduplicate_and_sort(source_tree: Path) -> None:
    found = indexer.search_symbols(source_tree, ["old_sum", "return", "old_sum"])
    assert {m.symbol for m in found} == {"old_sum", "return"}
    assert len(found) == 5
    assert found == sorted(found, key=lambda m: (m.file_path, m.line_number, m.symbol, m.snippet))


def test_excludes_non_source_trees(source_tree: Path) -> None:
    for directory in ("build", "vendor", "generated", ".cache", ".git", "third_party"):
        path = source_tree / directory
        path.mkdir()
        (path / "bad.cpp").write_text("old_sum();\n", encoding="utf-8")
    assert len(indexer.search_symbols(source_tree, ["old_sum"])) == 4


def test_empty_and_no_match_are_clean(source_tree: Path) -> None:
    assert indexer.search_symbols(source_tree, []) == []
    assert indexer.search_symbols(source_tree, ["absent_api"]) == []


def test_missing_rg_is_explicit(source_tree: Path) -> None:
    with pytest.raises(indexer.RipgrepUnavailable):
        indexer.search_symbols(source_tree, ["old_sum"], rg_executable="definitely-not-rg")


def test_timeout_is_explicit(source_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    process.returncode = None
    monkeypatch.setattr(indexer.subprocess, "Popen", lambda *a, **k: process)
    with pytest.raises(indexer.RipgrepTimeout):
        indexer.search_symbols(source_tree, ["old_sum"], timeout_seconds=0.01)


def test_malformed_json_is_explicit(source_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexer.subprocess, "Popen", lambda *a, **k: FakeProcess(b"not json\n"))
    with pytest.raises(indexer.RipgrepOutputError, match="malformed"):
        indexer.search_symbols(source_tree, ["old_sum"])


def test_nonzero_error_status_is_not_no_match(source_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexer.subprocess, "Popen", lambda *a, **k: FakeProcess(stderr=b"bad args", returncode=2))
    with pytest.raises(indexer.RipgrepExecutionError):
        indexer.search_symbols(source_tree, ["old_sum"])


def test_output_path_escape_is_rejected(source_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"type": "match", "data": {"path": {"text": "../escape.cpp"}, "lines": {"text": "x old_sum\n"}, "line_number": 1}}
    raw = (json.dumps(data) + "\n").encode()
    monkeypatch.setattr(indexer.subprocess, "Popen", lambda *a, **k: FakeProcess(raw))
    with pytest.raises(indexer.RipgrepOutputError, match="unsafe path"):
        indexer.search_symbols(source_tree, ["old_sum"])


def test_oversized_output_terminates_child(source_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = json.dumps({"type": "context", "data": {"lines": {"text": "x" * 500}}}).encode() + b"\n"
    process = FakeProcess(record * 10)
    process.returncode = None
    monkeypatch.setattr(indexer.subprocess, "Popen", lambda *a, **k: process)
    with pytest.raises(indexer.RipgrepOutputError, match="process terminated"):
        indexer.search_symbols(source_tree, ["old_sum"], max_output_bytes=20)
    assert process.terminated


def test_symlink_escape_is_not_returned(source_tree: Path, tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-outside.cpp"
    external.write_text("old_sum();\n", encoding="utf-8")
    try:
        (source_tree / "src" / "linked.cpp").symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not supported")
    found = indexer.search_symbols(source_tree, ["old_sum"])
    assert all("linked.cpp" not in m.file_path for m in found)
