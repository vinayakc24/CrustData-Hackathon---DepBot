"""Runs real `conan` against a throwaway Conan home (skipped if conan is absent)."""

import shutil
from pathlib import Path

import pytest

from depbot.cli import main
from depbot.package_managers.base import minimal_env
from depbot.package_managers.conan import CONAN_HOME_ENV, ConanAdapter
from depbot.package_managers.conan_setup import SetupError, prepare_local_packages

needs_conan = pytest.mark.skipif(shutil.which("conan") is None, reason="conan not installed")



FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "toydep"

def test_minimal_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    env = minimal_env({"CONAN_HOME": "/x"})
    assert "GITHUB_TOKEN" not in env and "LLM_API_KEY" not in env
    assert env["CONAN_HOME"] == "/x" and "PATH" in env


def test_adapter_env_follows_depbot_conan_home(monkeypatch, tmp_path):
    monkeypatch.delenv(CONAN_HOME_ENV, raising=False)
    assert ConanAdapter().command_env() == {"CMAKE_EXPORT_COMPILE_COMMANDS": "ON"}
    monkeypatch.setenv(CONAN_HOME_ENV, str(tmp_path))
    assert ConanAdapter().command_env() == {
        "CMAKE_EXPORT_COMPILE_COMMANDS": "ON", "CONAN_HOME": str(tmp_path.resolve())}


def test_rejects_non_recipe(tmp_path):
    with pytest.raises(SetupError):
        prepare_local_packages([tmp_path], tmp_path / "home")


@needs_conan
def test_demo_setup_builds_both_versions_into_isolated_home(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(CONAN_HOME_ENV, raising=False)
    fleet = Path(__file__).resolve().parents[2] / "examples" / "demo-fleet.yaml"
    recipe_v1, recipe_v2 = FIXTURE_ROOT / "v1", FIXTURE_ROOT / "v2"
    state_dir = tmp_path / "state"
    code = main([
        "demo", "setup", "--fleet", str(fleet), "--state-dir", str(state_dir),
        "--recipe", str(recipe_v1), "--recipe", str(recipe_v2),
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    for label in ("create v1", "create v2"):
        assert any(line.lstrip().startswith(label) and "ok" in line for line in out.splitlines()), out
    home = state_dir / "conan"
    assert (home / "profiles" / "default").is_file()
    import subprocess
    listing = subprocess.run(["conan", "list", "toydep/*"], env=minimal_env({"CONAN_HOME": str(home)}),
                             capture_output=True, text=True, timeout=60).stdout
    assert "toydep/1.0.0" in listing and "toydep/2.0.0" in listing

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
