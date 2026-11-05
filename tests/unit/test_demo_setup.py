"""Runs real `conan` against a throwaway Conan home (skipped if conan is absent)."""

import shutil
import textwrap

import pytest

from depbot.cli import main
from depbot.package_managers.base import minimal_env
from depbot.package_managers.conan import CONAN_HOME_ENV, ConanAdapter
from depbot.package_managers.conan_setup import SetupError, prepare_local_packages

needs_conan = pytest.mark.skipif(shutil.which("conan") is None, reason="conan not installed")


def _recipe(root, version):
    d = root / f"toydep-{version}"
    (d / "include" / "toydep").mkdir(parents=True)
    (d / "src").mkdir()
    fn = "old_sum" if version.startswith("1") else "sum"
    (d / "include" / "toydep" / "toydep.h").write_text(f"#pragma once\nint {fn}(int a, int b);\n")
    (d / "src" / "toydep.cpp").write_text(f'#include "toydep/toydep.h"\nint {fn}(int a, int b) {{ return a + b; }}\n')
    (d / "CMakeLists.txt").write_text(textwrap.dedent("""\
        cmake_minimum_required(VERSION 3.15)
        project(toydep CXX)
        add_library(toydep src/toydep.cpp)
        target_include_directories(toydep PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include> $<INSTALL_INTERFACE:include>)
        install(TARGETS toydep)
        install(DIRECTORY include/ DESTINATION include)
        """))
    (d / "conanfile.py").write_text(textwrap.dedent(f"""\
        from conan import ConanFile
        from conan.tools.cmake import CMake, cmake_layout
        class Toydep(ConanFile):
            name = "toydep"
            version = "{version}"
            settings = "os", "compiler", "build_type", "arch"
            generators = "CMakeToolchain", "CMakeDeps"
            exports_sources = "CMakeLists.txt", "src/*", "include/*"
            def layout(self): cmake_layout(self)
            def build(self):
                c = CMake(self); c.configure(); c.build()
            def package(self): CMake(self).install()
            def package_info(self): self.cpp_info.libs = ["toydep"]
        """))
    return d


def test_minimal_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    env = minimal_env({"CONAN_HOME": "/x"})
    assert "GITHUB_TOKEN" not in env and "LLM_API_KEY" not in env
    assert env["CONAN_HOME"] == "/x" and "PATH" in env


def test_adapter_env_follows_depbot_conan_home(monkeypatch, tmp_path):
    monkeypatch.delenv(CONAN_HOME_ENV, raising=False)
    assert ConanAdapter().command_env() == {}
    monkeypatch.setenv(CONAN_HOME_ENV, str(tmp_path))
    assert ConanAdapter().command_env() == {"CONAN_HOME": str(tmp_path.resolve())}


def test_rejects_non_recipe(tmp_path):
    with pytest.raises(SetupError):
        prepare_local_packages([tmp_path], tmp_path / "home")


@needs_conan
def test_demo_setup_builds_both_versions_into_isolated_home(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(CONAN_HOME_ENV, raising=False)
    (tmp_path / "fleet.yaml").write_text("fleet_id: f\nrepositories: []\n")
    r1, r2 = _recipe(tmp_path, "1.0.0"), _recipe(tmp_path, "2.0.0")
    code = main(["demo", "setup", "--fleet", str(tmp_path / "fleet.yaml"),
                 "--recipe", str(r1), "--recipe", str(r2)])
    out = capsys.readouterr().out
    assert code == 0, out
    home = tmp_path / ".depbot" / "conan"
    assert (home / "profiles" / "default").is_file()
    import subprocess
    listing = subprocess.run(["conan", "list", "toydep/*"], env=minimal_env({"CONAN_HOME": str(home)}),
                             capture_output=True, text=True, timeout=60).stdout
    assert "toydep/1.0.0" in listing and "toydep/2.0.0" in listing

    # Contract C relies on: install_args + command_env resolve offline in the isolated home.
    app = tmp_path / "app"
    app.mkdir()
    (app / "conanfile.txt").write_text(
        "[requires]\ntoydep/1.0.0\n\n[generators]\nCMakeDeps\nCMakeToolchain\n\n[layout]\ncmake_layout\n")
    monkeypatch.setenv(CONAN_HOME_ENV, str(home))
    adapter = ConanAdapter()
    proc = subprocess.run(adapter.install_args(app), cwd=app, env=minimal_env(adapter.command_env()),
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert (app / "build" / "Release" / "generators" / "toydep-config.cmake").is_file()
