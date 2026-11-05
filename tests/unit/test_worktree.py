import os
import subprocess

import pytest

from depbot.repository.paths import UnsafePath, is_allowed, safe_join
from depbot.repository.worktree import (
    UnsafeRepository,
    branch_name,
    changed_files,
    full_diff,
    prepare_working_copy,
)


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "src_repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.cpp").write_text("int main(){return old_sum(1,2)-3;}\n")
    (repo / "conanfile.txt").write_text("[requires]\ntoydep/1.0.0\n")
    (repo / "build").mkdir()
    (repo / "build" / "junk.o").write_text("x")
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_copy_is_isolated_and_records_only_depbot_edits(source, tmp_path):
    before = _snapshot(source)
    wc = prepare_working_copy(source, tmp_path / "art" / "wt", branch=branch_name("toydep", "2.0.0"))
    wt = tmp_path / "art" / "wt"
    assert wc.branch == "depbot/toydep-2.0.0" and wc.source_commit and wc.version_controlled
    assert not (wt / "build").exists()  # build dirs are not copied
    (wt / "src" / "main.cpp").write_text("int main(){return sum(1,2)-3;}\n")
    (wt / "build").mkdir()
    (wt / "build" / "out.o").write_text("y")  # build output is ignored
    (wt / "src" / "new.h").write_text("// new\n")
    assert changed_files(wt) == ["src/main.cpp", "src/new.h"]
    assert "+int main(){return sum(1,2)-3;}" in full_diff(wt)
    assert _snapshot(source) == before  # original never modified


def test_dirty_source_refused_by_default(source, tmp_path):
    (source / "src" / "main.cpp").write_text("// local edit\n")
    with pytest.raises(UnsafeRepository, match="uncommitted"):
        prepare_working_copy(source, tmp_path / "wt", branch="depbot/x")
    assert not (tmp_path / "wt").exists()
    wc = prepare_working_copy(source, tmp_path / "wt2", branch="depbot/x", refuse_dirty=False)
    assert "uncommitted" in wc.notes[0]


def test_destination_rules_and_symlink_escape(source, tmp_path):
    with pytest.raises(UnsafeRepository, match="inside the source"):
        prepare_working_copy(source, source / "art", branch="depbot/x")
    (tmp_path / "exists").mkdir()
    with pytest.raises(UnsafeRepository, match="already exists"):
        prepare_working_copy(source, tmp_path / "exists", branch="depbot/x")
    os.symlink("/etc/passwd", source / "src" / "leak")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "link")
    with pytest.raises(UnsafeRepository, match="symlink escapes"):
        prepare_working_copy(source, tmp_path / "wt3", branch="depbot/x")


def test_non_git_source_is_copied_with_note(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.cpp").write_text("x\n")
    wc = prepare_working_copy(plain, tmp_path / "wt", branch="depbot/x")
    assert not wc.version_controlled and "not under Git" in wc.notes[0]


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", ".git/config", "", "C:/x", "src/.env"])
def test_safe_join_rejects(tmp_path, bad):
    with pytest.raises(UnsafePath):
        safe_join(tmp_path, bad)


def test_safe_join_and_allow_list(tmp_path):
    assert safe_join(tmp_path, "src/main.cpp") == (tmp_path / "src" / "main.cpp").resolve()
    assert is_allowed("src/main.cpp", ["src", "conanfile.txt"])
    assert is_allowed("conanfile.txt", ["src", "conanfile.txt"])
    assert not is_allowed("srcx/main.cpp", ["src"])
    assert not is_allowed("CMakeLists.txt", [])
