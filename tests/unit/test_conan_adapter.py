import json

import pytest

from depbot.package_managers.base import AmbiguousDependency, DependencyNotFound, UnsupportedManifest
from depbot.package_managers.conan import ConanAdapter
from depbot.package_managers.detector import detect_adapter

MANIFEST = """\
# fixture manifest
[requires]
toydep/1.0.0
zlib/1.3.1@acme/stable#0123abcdef

[tool_requires]
cmake/3.28.1

[generators]
CMakeDeps
CMakeToolchain

[layout]
cmake_layout
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "conanfile.txt").write_text(MANIFEST)
    return tmp_path


def test_reads_direct_requirements_ignoring_other_sections(repo):
    deps = ConanAdapter().read_dependencies(repo)
    assert [(d.name, d.version, d.section) for d in deps] == [
        ("toydep", "1.0.0", "requires"),
        ("zlib", "1.3.1", "requires"),
        ("cmake", "3.28.1", "tool_requires"),
    ]


def test_update_changes_only_selected_dependency(repo):
    change = ConanAdapter().update_dependency(repo, "toydep", "2.0.0")
    text = (repo / "conanfile.txt").read_text()
    assert "toydep/2.0.0" in text and "toydep/1.0.0" not in text
    assert text.replace("toydep/2.0.0", "toydep/1.0.0") == MANIFEST
    assert change.from_version == "1.0.0" and change.to_version == "2.0.0"
    assert "-toydep/1.0.0" in change.unified_diff and "+toydep/2.0.0" in change.unified_diff


def test_update_preserves_user_channel_and_revision(repo):
    ConanAdapter().update_dependency(repo, "zlib", "1.3.2")
    assert "zlib/1.3.2@acme/stable#0123abcdef" in (repo / "conanfile.txt").read_text()


def test_missing_and_ambiguous_dependencies_refused(repo):
    with pytest.raises(DependencyNotFound):
        ConanAdapter().update_dependency(repo, "fmt", "11.0.0")
    (repo / "conanfile.txt").write_text("[requires]\ntoydep/1.0.0\ntoydep/1.1.0\n")
    with pytest.raises(AmbiguousDependency):
        ConanAdapter().update_dependency(repo, "toydep", "2.0.0")
    assert "toydep/1.1.0" in (repo / "conanfile.txt").read_text()  # untouched


def test_version_ranges_and_bad_targets_refused(tmp_path):
    (tmp_path / "conanfile.txt").write_text("[requires]\ntoydep/[>=1.0 <2]\n")
    with pytest.raises(UnsupportedManifest):
        ConanAdapter().read_dependencies(tmp_path)
    (tmp_path / "conanfile.txt").write_text("[requires]\ntoydep/1.0.0\n")
    with pytest.raises(UnsupportedManifest):
        ConanAdapter().update_dependency(tmp_path, "toydep", "2.0.0; rm -rf /")


def test_detector_refuses_conanfile_py_and_mixed_managers(tmp_path):
    (tmp_path / "conanfile.py").write_text("print('x')")
    with pytest.raises(UnsupportedManifest, match="executable"):
        detect_adapter(tmp_path)
    (tmp_path / "conanfile.py").unlink()
    (tmp_path / "conanfile.txt").write_text("[requires]\ntoydep/1.0.0\n")
    (tmp_path / "vcpkg.json").write_text("{}")
    with pytest.raises(UnsupportedManifest, match="multiple package managers"):
        detect_adapter(tmp_path)
    with pytest.raises(UnsupportedManifest, match="no supported"):
        detect_adapter(tmp_path / "missing")


def test_install_args_are_argv_lists(repo):
    adapter = detect_adapter(repo)
    assert adapter.install_args(repo, build_type="Debug") == [
        "conan", "install", ".", "--build=missing", "-s", "build_type=Debug"
    ]
    assert adapter.cmake_preset(build_type="Release") == "conan-release"


# ---- repository-shape robustness (examples/shapes; real run in tests/e2e/test_repo_shapes.py)

def _conan_repo(tmp_path, text, lock=None):
    (tmp_path / "conanfile.txt").write_bytes(text.encode())
    if lock is not None:
        (tmp_path / "conan.lock").write_text(json.dumps(lock, indent=4) + "\n")
    return tmp_path


def test_crlf_conanfile_is_edited_byte_exactly(tmp_path):
    text = "[requires]\r\notherdep/1.0.0\r\ntoydep/1.0.0\r\n\r\n[generators]\r\nCMakeDeps\r\n"
    repo = _conan_repo(tmp_path, text)
    ConanAdapter().update_dependency(repo, "toydep", "2.0.0")
    assert (repo / "conanfile.txt").read_bytes() == text.replace("toydep/1.0.0", "toydep/2.0.0").encode()


def test_version_range_refusal_names_the_range(tmp_path):
    repo = _conan_repo(tmp_path, "[requires]\ntoydep/[>=1.0 <2.0]\n")
    with pytest.raises(UnsupportedManifest, match="version range .* cannot be updated safely"):
        ConanAdapter().read_dependencies(repo)


def test_lockfile_unpins_only_the_upgraded_dependency(tmp_path):
    lock = {"version": "0.5", "requires": ["toydep/1.0.0#aa%1", "otherdep/1.0.0#bb%2", "toydep_extra/3.0#cc%3"],
            "build_requires": ["toydep/1.0.0#aa%1"], "python_requires": [], "config_requires": []}
    repo = _conan_repo(tmp_path, "[requires]\notherdep/1.0.0\ntoydep/1.0.0\n", lock)
    adapter = ConanAdapter()
    assert adapter.lockfiles(repo) == ["conan.lock"]
    adapter.update_dependency(repo, "toydep", "2.0.0")
    after = json.loads((repo / "conan.lock").read_text())
    assert after["requires"] == ["otherdep/1.0.0#bb%2", "toydep_extra/3.0#cc%3"] and after["build_requires"] == []
    assert adapter.install_args(repo)[-5:] == ["--lockfile", "conan.lock", "--lockfile-partial",
                                                "--lockfile-out", "conan.lock"]


def test_no_lockfile_means_plain_install(tmp_path):
    repo = _conan_repo(tmp_path, "[requires]\ntoydep/1.0.0\n")
    assert ConanAdapter().lockfiles(repo) == [] and "--lockfile" not in ConanAdapter().install_args(repo)


def test_invalid_lockfile_is_a_safe_refusal(tmp_path):
    repo = _conan_repo(tmp_path, "[requires]\ntoydep/1.0.0\n")
    (repo / "conan.lock").write_text("{not json")
    with pytest.raises(UnsupportedManifest, match="conan.lock is not valid JSON"):
        ConanAdapter().update_dependency(repo, "toydep", "2.0.0")
