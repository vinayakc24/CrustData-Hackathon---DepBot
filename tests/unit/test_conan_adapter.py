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
