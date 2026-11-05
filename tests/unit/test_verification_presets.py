"""CMake preset discovery in verification (fix for Conan's CMakeUserPresets.json layout)."""

import json

import pytest

from depbot.interfaces import StageError
from depbot.verification.pipeline import _preset_build_dir


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


def _conan_layout(wt):
    """What `conan install` with cmake_layout writes (Conan 2)."""
    _write(wt / "CMakeUserPresets.json", {"version": 4, "include": ["build/Release/generators/CMakePresets.json"]})
    _write(wt / "build" / "Release" / "generators" / "CMakePresets.json", {
        "version": 3,
        "configurePresets": [{"name": "conan-release", "binaryDir": str(wt / "build" / "Release")}],
    })


def test_conan_user_presets_include_chain_is_followed(tmp_path):
    _conan_layout(tmp_path)
    assert _preset_build_dir(tmp_path, "conan-release") == (tmp_path / "build" / "Release").resolve()
    assert _preset_build_dir(tmp_path, "conan-debug") is None


def test_project_presets_still_work_and_win(tmp_path):
    _write(tmp_path / "CMakePresets.json", {"version": 3, "configurePresets": [
        {"name": "conan-release", "binaryDir": "${sourceDir}/out/${presetName}"}]})
    _conan_layout(tmp_path)
    assert _preset_build_dir(tmp_path, "conan-release") == (tmp_path / "out" / "conan-release").resolve()


def test_includes_outside_worktree_cycles_and_bad_json_are_ignored(tmp_path):
    outside = tmp_path / "outside.json"
    _write(outside, {"configurePresets": [{"name": "conan-release", "binaryDir": str(tmp_path / "evil")}]})
    wt = tmp_path / "wt"
    _write(wt / "CMakeUserPresets.json", {"include": ["../outside.json", "a.json", "broken.json"]})
    _write(wt / "a.json", {"include": ["CMakeUserPresets.json"]})  # cycle back
    _write(wt / "broken.json", "{not json")
    assert _preset_build_dir(wt, "conan-release") is None


def test_binary_dir_escaping_worktree_is_refused(tmp_path):
    _write(tmp_path / "CMakeUserPresets.json", {"include": ["p.json"]})
    _write(tmp_path / "p.json", {"configurePresets": [{"name": "conan-release", "binaryDir": "/tmp/elsewhere"}]})
    with pytest.raises(StageError, match="escapes the isolated worktree"):
        _preset_build_dir(tmp_path, "conan-release")


def test_no_presets_at_all(tmp_path):
    assert _preset_build_dir(tmp_path, "conan-release") is None
