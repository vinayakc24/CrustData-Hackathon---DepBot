"""Real CMake install/configure/build/CTest pipeline."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from depbot.models import (
    CommandResult,
    UpgradePolicy,
    VerificationPhase,
    VerificationResult,
)
from depbot.interfaces import StageError
from depbot.package_managers.base import PackageManagerAdapter, minimal_env
from depbot.verification.diagnostics import diagnostics_for
from depbot.verification.command_runner import run_command
from depbot.verification.error_classifier import classify_failure


INSTALL_TIMEOUT_SECONDS = 600
CONFIGURE_TIMEOUT_SECONDS = 180
BUILD_TIMEOUT_SECONDS = 600
TEST_TIMEOUT_SECONDS = 300


def _preset_entries(worktree: Path):
    """configurePresets from CMakePresets.json and CMakeUserPresets.json, following `include`
    chains (Conan 2 writes CMakeUserPresets.json including build/<T>/generators/CMakePresets.json)."""
    pending = [worktree / "CMakePresets.json", worktree / "CMakeUserPresets.json"]
    seen: set[Path] = set()
    while pending:
        path = pending.pop(0).resolve()
        if path in seen or not path.is_file() or worktree.resolve() not in path.parents:
            continue
        seen.add(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        yield from data.get("configurePresets", [])
        pending.extend(path.parent / inc for inc in data.get("include", []) if isinstance(inc, str))


def _preset_build_dir(worktree: Path, preset: str) -> Path | None:
    for entry in _preset_entries(worktree):
        if entry.get("name") != preset:
            continue
        value = entry.get("binaryDir")
        if not isinstance(value, str):
            return None
        value = (
            value.replace("${sourceDir}", str(worktree))
            .replace("${sourceParentDir}", str(worktree.parent))
            .replace("${sourceDirName}", worktree.name)
            .replace("${sourceParentDirName}", worktree.parent.name)
            .replace("${presetName}", preset)
        )
        if "${" in value or "$env{" in value or "$penv{" in value:
            raise StageError(
                f"CMake preset {preset!r} uses an unsupported binaryDir macro; refusing unsafe build location",
                blocked=True,
            )
        directory = Path(value)
        directory = (directory if directory.is_absolute() else worktree / directory).resolve()
        if directory == worktree or worktree not in directory.parents:
            raise StageError(
                f"CMake preset {preset!r} binaryDir escapes the isolated worktree: {directory}",
                blocked=True,
            )
        return directory
    return None


def _reset_build_dir(worktree: Path) -> None:
    """Recreate the isolated build tree so phases cannot reuse stale CMake caches."""
    build_dir = worktree / "build"
    if build_dir.is_symlink():
        raise StageError("refusing to reset a symlinked build directory", blocked=True)
    resolved = build_dir.resolve()
    if resolved == worktree or worktree not in resolved.parents:
        raise StageError(f"build directory escapes the isolated worktree: {resolved}", blocked=True)
    try:
        if build_dir.exists():
            if build_dir.is_dir():
                shutil.rmtree(build_dir)
            else:
                build_dir.unlink()
        build_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StageError(
            f"could not recreate isolated build directory {build_dir}: {exc}", blocked=True
        ) from exc


def _run_step(
    *,
    label: str,
    args: list[str],
    worktree: Path,
    artifacts_dir: Path,
    timeout: float,
    extra_env: dict[str, str],
) -> CommandResult:
    return run_command(
        command_label=label,
        args=args,
        cwd=worktree,
        artifacts_dir=artifacts_dir,
        timeout_seconds=timeout,
        extra_env=extra_env,
    )


def _test_counts(result: CommandResult) -> tuple[int | None, int | None, list[str]]:
    output = f"{result.stdout_tail}\n{result.stderr_tail}"
    match = re.search(r"(\d+) tests? failed out of (\d+)", output, re.IGNORECASE)
    if match:
        failed, total = int(match.group(1)), int(match.group(2))
        names: list[str] = []
        if failed:
            section = re.search(r"The following tests FAILED:\s*(.*?)(?:\n\s*\n|\Z)", output, re.DOTALL)
            if section:
                for line in section.group(1).splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    named = re.match(r"\d+\s*[-:]\s*(.+?)(?:\s+\(Failed\))?$", line, re.IGNORECASE)
                    names.append(named.group(1).strip() if named else line)
        return max(0, total - failed), failed, names
    passed_match = re.search(r"(\d+) tests? passed out of (\d+)", output, re.IGNORECASE)
    if passed_match:
        passed, total = int(passed_match.group(1)), int(passed_match.group(2))
        return passed, max(0, total - passed), []
    percent_match = re.search(r"(\d+)% tests? passed out of (\d+)", output, re.IGNORECASE)
    if percent_match:
        percent, total = int(percent_match.group(1)), int(percent_match.group(2))
        passed = round(total * percent / 100)
        return passed, max(0, total - passed), []
    if "no tests were found" in output.lower():
        return 0, 0, []
    return (0, 0, []) if result.succeeded else (None, None, [])


def run_verification(
    *,
    worktree: Path,
    adapter: PackageManagerAdapter,
    phase: VerificationPhase,
    policy: UpgradePolicy,
    artifacts_dir: Path,
) -> VerificationResult:
    """Run package resolution followed by CMake configure, build and CTest.

    Each phase stops at its first failed prerequisite. A required test suite with
    zero discovered tests is reported as unpassed.
    """
    worktree = worktree.resolve()
    _reset_build_dir(worktree)
    logs_dir = artifacts_dir / "commands" / phase.value
    logs_dir.mkdir(parents=True, exist_ok=True)
    extra_env = minimal_env(adapter.command_env())
    results: dict[str, CommandResult | None] = {"install": None, "configure": None, "build": None, "tests": None}

    try:
        install_args = adapter.install_args(worktree, build_type="Release")
    except Exception as exc:
        # Adapter refusal is an observed install-stage failure and is not hidden.
        result = run_command(
            command_label=f"{phase.value}.dependency_install",
            args=[],
            cwd=worktree,
            artifacts_dir=logs_dir,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            extra_env=extra_env,
        ).model_copy(update={"stderr_tail": f"adapter install_args failed: {type(exc).__name__}: {exc}"})
        results["install"] = result
    else:
        results["install"] = run_command(
            command_label=f"{phase.value}.dependency_install",
            args=install_args,
            cwd=worktree,
            artifacts_dir=logs_dir,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            extra_env=extra_env,
        )

    preset = adapter.cmake_preset(build_type="Release")
    preset_dir = _preset_build_dir(worktree, preset) if preset else None
    build_dir = preset_dir or (worktree / "build" / phase.value)
    if results["install"] is not None and results["install"].succeeded:
        if preset and preset_dir is not None:
            configure_args = ["cmake", "--preset", preset]
        else:
            configure_args = [
                "cmake", "-S", str(worktree), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"
            ]
            configure_args.extend(
                adapter.cmake_configure_args(worktree, build_type="Release")
            )
        results["configure"] = _run_step(
            label=f"{phase.value}.configure", args=configure_args, worktree=worktree,
            artifacts_dir=logs_dir, timeout=CONFIGURE_TIMEOUT_SECONDS, extra_env=extra_env,
        )

    if results["configure"] is not None and results["configure"].succeeded:
        build_args = (
            ["cmake", "--build", "--preset", preset, "--config", "Release"]
            if preset and preset_dir is not None
            else ["cmake", "--build", str(build_dir), "--config", "Release", "--parallel", "2"]
        )
        results["build"] = _run_step(
            label=f"{phase.value}.build", args=build_args, worktree=worktree,
            artifacts_dir=logs_dir, timeout=BUILD_TIMEOUT_SECONDS, extra_env=extra_env,
        )

    if results["build"] is not None and results["build"].succeeded:
        test_args = [
            "ctest", "--test-dir", str(build_dir), "-C", "Release", "--output-on-failure"
        ]
        results["tests"] = _run_step(
            label=f"{phase.value}.tests", args=test_args, worktree=worktree,
            artifacts_dir=logs_dir, timeout=TEST_TIMEOUT_SECONDS, extra_env=extra_env,
        )

    tests_passed = tests_failed = None
    failed_names: list[str] = []
    if results["tests"] is not None:
        tests_passed, tests_failed, failed_names = _test_counts(results["tests"])

    step_ok = all(
        results[name] is not None and results[name].succeeded
        for name in ("install", "configure", "build")
    )
    tests_ok = (
        results["tests"] is not None
        and results["tests"].succeeded
        and tests_failed == 0
        and (not policy.require_tests or (tests_passed is not None and tests_passed > 0))
    )
    diagnostics = diagnostics_for(
        [results["configure"], results["build"], results["tests"]],
        worktree=worktree, build_dirs=[build_dir],
    )
    verification = VerificationResult(
        phase=phase,
        diagnostics=diagnostics,
        dependency_install=results["install"],
        configure=results["configure"],
        build=results["build"],
        tests=results["tests"],
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        failed_test_names=failed_names,
        passed=step_ok and (tests_ok or (not policy.require_tests and results["tests"] is not None and results["tests"].succeeded)),
        failure_category=None,
    )
    return verification.model_copy(update={"failure_category": classify_failure(verification)})
