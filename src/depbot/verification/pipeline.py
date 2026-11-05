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
    """Run the isolated Debug and Release CMake verification configurations.

    The frozen ``VerificationResult`` schema exposes one set of command fields,
    which continues to describe Release for compatibility. Full per-configuration
    evidence is written to ``verification/<phase>/configuration-matrix.json``.
    The phase passes only if both configurations pass.
    """
    worktree = worktree.resolve()
    # Clear stale caches from the prior verification phase, then retain both
    # configuration-specific trees so both compile databases remain available.
    _reset_build_dir(worktree)
    extra_env = minimal_env(adapter.command_env())
    configurations: dict[str, dict] = {}
    for build_type in ("Release", "Debug"):
        # Conan and CMake generate configuration-specific toolchains/presets.
        configurations[build_type] = _run_configuration(
            worktree=worktree,
            adapter=adapter,
            phase=phase,
            build_type=build_type,
            policy=policy,
            artifacts_dir=artifacts_dir,
            extra_env=extra_env,
        )

    matrix_path = artifacts_dir / "verification" / phase.value / "configuration-matrix.json"
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    matrix_path.write_text(
        json.dumps(
            {
                "phase": phase.value,
                "passed": all(item["passed"] for item in configurations.values()),
                "configurations": {
                    name: {
                        "passed": item["passed"],
                        "tests_passed": item["tests_passed"],
                        "tests_failed": item["tests_failed"],
                        "failed_test_names": item["failed_test_names"],
                        "commands": {
                            key: result.model_dump(mode="json") if result else None
                            for key, result in item["results"].items()
                        },
                    }
                    for name, item in configurations.items()
                },
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    # Keep the contract's legacy fields tied to Release when it passes. If one
    # configuration fails, expose that failure in the legacy fields so existing
    # outcome classification and repair logic see the actual failing command.
    selected = next(
        (item for item in configurations.values() if not item["passed"]),
        configurations["Release"],
    )
    results = selected["results"]
    tests_passed = selected["tests_passed"]
    tests_failed = selected["tests_failed"]
    failed_names = selected["failed_test_names"]
    diagnostics = [diagnostic for item in configurations.values() for diagnostic in item["diagnostics"]]
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
        passed=all(item["passed"] for item in configurations.values()),
        failure_category=None,
    )
    failure_category = next(
        (
            item["failure_category"]
            for item in configurations.values()
            if not item["passed"] and item["failure_category"]
        ),
        None,
    )
    return verification.model_copy(
        update={"failure_category": failure_category or classify_failure(verification)}
    )


def _run_configuration(
    *,
    worktree: Path,
    adapter: PackageManagerAdapter,
    phase: VerificationPhase,
    build_type: str,
    policy: UpgradePolicy,
    artifacts_dir: Path,
    extra_env: dict[str, str],
) -> dict:
    """Run one independent package install/configure/build/test configuration."""
    config = build_type.lower()
    logs_dir = artifacts_dir / "commands" / phase.value / config
    logs_dir.mkdir(parents=True, exist_ok=True)
    labels = {
        "Release": {
            "install": f"{phase.value}.dependency_install",
            "configure": f"{phase.value}.configure",
            "build": f"{phase.value}.build",
            "tests": f"{phase.value}.tests",
        },
        "Debug": {
            "install": f"{phase.value}.debug.dependency_install",
            "configure": f"{phase.value}.debug.configure",
            "build": f"{phase.value}.debug.build",
            "tests": f"{phase.value}.debug.tests",
        },
    }[build_type]
    results: dict[str, CommandResult | None] = {
        "install": None, "configure": None, "build": None, "tests": None
    }

    try:
        install_args = adapter.install_args(worktree, build_type=build_type)
    except Exception as exc:
        results["install"] = run_command(
            command_label=labels["install"], args=[], cwd=worktree,
            artifacts_dir=logs_dir, timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            extra_env=extra_env,
        ).model_copy(update={"stderr_tail": f"adapter install_args failed: {type(exc).__name__}: {exc}"})
    else:
        results["install"] = run_command(
            command_label=labels["install"], args=install_args, cwd=worktree,
            artifacts_dir=logs_dir, timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            extra_env=extra_env,
        )

    preset = adapter.cmake_preset(build_type=build_type)
    preset_dir = _preset_build_dir(worktree, preset) if preset else None
    build_dir = preset_dir or (worktree / "build" / phase.value / config)
    if results["install"] is not None and results["install"].succeeded:
        if preset and preset_dir is not None:
            configure_args = ["cmake", "--preset", preset]
        else:
            configure_args = [
                "cmake", "-S", str(worktree), "-B", str(build_dir),
                f"-DCMAKE_BUILD_TYPE={build_type}",
            ]
            configure_args.extend(adapter.cmake_configure_args(worktree, build_type=build_type))
        results["configure"] = _run_step(
            label=labels["configure"], args=configure_args, worktree=worktree,
            artifacts_dir=logs_dir, timeout=CONFIGURE_TIMEOUT_SECONDS, extra_env=extra_env,
        )

    if results["configure"] is not None and results["configure"].succeeded:
        build_args = (
            ["cmake", "--build", "--preset", preset, "--config", build_type]
            if preset and preset_dir is not None
            else ["cmake", "--build", str(build_dir), "--config", build_type, "--parallel", "2"]
        )
        results["build"] = _run_step(
            label=labels["build"], args=build_args, worktree=worktree,
            artifacts_dir=logs_dir, timeout=BUILD_TIMEOUT_SECONDS, extra_env=extra_env,
        )

    if results["build"] is not None and results["build"].succeeded:
        test_args = [
            "ctest", "--test-dir", str(build_dir), "-C", build_type, "--output-on-failure"
        ]
        results["tests"] = _run_step(
            label=labels["tests"], args=test_args, worktree=worktree,
            artifacts_dir=logs_dir, timeout=TEST_TIMEOUT_SECONDS, extra_env=extra_env,
        )

    tests_passed = tests_failed = None
    failed_names: list[str] = []
    if results["tests"] is not None:
        tests_passed, tests_failed, failed_names = _test_counts(results["tests"])
    step_ok = all(
        results[key] is not None and results[key].succeeded
        for key in ("install", "configure", "build")
    )
    tests_ok = (
        results["tests"] is not None and results["tests"].succeeded
        and tests_failed == 0
        and (not policy.require_tests or (tests_passed is not None and tests_passed > 0))
    )
    diagnostics = diagnostics_for(
        [results["configure"], results["build"], results["tests"]],
        worktree=worktree, build_dirs=[build_dir],
    )
    check = VerificationResult(
        phase=phase,
        diagnostics=diagnostics,
        dependency_install=results["install"],
        configure=results["configure"],
        build=results["build"],
        tests=results["tests"],
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        failed_test_names=failed_names,
        passed=step_ok and (
            tests_ok or (
                not policy.require_tests and results["tests"] is not None
                and results["tests"].succeeded
            )
        ),
    )
    check = check.model_copy(update={"failure_category": classify_failure(check)})
    return {
        "results": results,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "failed_test_names": failed_names,
        "passed": check.passed,
        "failure_category": check.failure_category,
        "diagnostics": diagnostics,
    }
