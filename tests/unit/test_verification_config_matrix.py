import json
from pathlib import Path
from types import SimpleNamespace

from depbot.models import CommandResult, UpgradePolicy, VerificationPhase
from depbot.verification.pipeline import run_verification


def test_verification_runs_release_and_debug_and_records_matrix(tmp_path, monkeypatch):
    import depbot.verification.pipeline as pipeline

    calls = []

    def successful_step(*, label, args, worktree, **_kwargs):
        calls.append((label, args))
        if label.endswith("configure"):
            Path(args[args.index("-B") + 1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(
            command_label=label,
            args=args,
            cwd=str(worktree),
            exit_code=0,
            duration_seconds=0.01,
            stdout_tail="1 test passed out of 1" if label.endswith(".tests") else "",
        )

    installs = []
    monkeypatch.setattr(pipeline, "_run_step", successful_step)
    monkeypatch.setattr(
        pipeline,
        "run_command",
        lambda **kwargs: (
            installs.append(kwargs["args"])
            or CommandResult(
                command_label=kwargs["command_label"],
                args=kwargs["args"],
                cwd=str(kwargs["cwd"]),
                exit_code=0,
                duration_seconds=0.01,
            )
        ),
    )
    adapter = SimpleNamespace(
        command_env=lambda: {},
        install_args=lambda _root, *, build_type: ["pkg", "install", build_type],
        cmake_preset=lambda *, build_type: None,
        cmake_configure_args=lambda _root, *, build_type: [],
    )

    result = run_verification(
        worktree=tmp_path,
        adapter=adapter,
        phase=VerificationPhase.UPGRADED,
        policy=UpgradePolicy(require_tests=True),
        artifacts_dir=tmp_path / "artifacts",
    )

    assert result.passed is True
    assert installs == [["pkg", "install", "Release"], ["pkg", "install", "Debug"]]
    for configuration in ("Release", "Debug"):
        assert any(
            label.endswith(f"{configuration.lower()}.configure")
            or (configuration == "Release" and label == "upgraded.configure")
            for label, _args in calls
        )
        assert any(
            args[-1] == f"-DCMAKE_BUILD_TYPE={configuration}"
            for label, args in calls
            if label.endswith("configure")
        )
        assert any(
            args[-3] == "-C" and args[-2] == configuration
            for label, args in calls
            if label.endswith(".tests")
        )

    matrix_path = tmp_path / "artifacts" / "verification" / "upgraded" / "configuration-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert matrix["passed"] is True
    assert matrix["configurations"]["Debug"]["passed"] is True
    assert matrix["configurations"]["Release"]["passed"] is True
    assert matrix["configurations"]["Debug"]["commands"]["build"]["command_label"] == "upgraded.debug.build"
    assert (tmp_path / "build" / "upgraded" / "release").is_dir()
    assert (tmp_path / "build" / "upgraded" / "debug").is_dir()
    assert (tmp_path / "artifacts" / "commands" / "upgraded" / "release").is_dir()
    assert (tmp_path / "artifacts" / "commands" / "upgraded" / "debug").is_dir()


def test_debug_failure_fails_verification_even_when_release_passes(tmp_path, monkeypatch):
    import depbot.verification.pipeline as pipeline

    def step(*, label, args, worktree, **_kwargs):
        code = 1 if label == "upgraded.debug.build" else 0
        return CommandResult(
            command_label=label,
            args=args,
            cwd=str(worktree),
            exit_code=code,
            duration_seconds=0.01,
        )

    monkeypatch.setattr(pipeline, "_run_step", step)
    monkeypatch.setattr(
        pipeline,
        "run_command",
        lambda **kwargs: CommandResult(
            command_label=kwargs["command_label"],
            args=kwargs["args"],
            cwd=str(kwargs["cwd"]),
            exit_code=0,
            duration_seconds=0.01,
        ),
    )
    adapter = SimpleNamespace(
        command_env=lambda: {},
        install_args=lambda _root, **_kwargs: ["pkg", "install"],
        cmake_preset=lambda **_kwargs: None,
        cmake_configure_args=lambda _root, **_kwargs: [],
    )

    result = run_verification(
        worktree=tmp_path,
        adapter=adapter,
        phase=VerificationPhase.UPGRADED,
        policy=UpgradePolicy(require_tests=False),
        artifacts_dir=tmp_path / "artifacts",
    )

    assert result.passed is False
    assert result.build is not None and not result.build.succeeded
    assert result.build.command_label == "upgraded.debug.build"
    matrix = json.loads(
        (tmp_path / "artifacts" / "verification" / "upgraded" / "configuration-matrix.json")
        .read_text(encoding="utf-8")
    )
    assert matrix["configurations"]["Release"]["passed"] is True
    assert matrix["configurations"]["Debug"]["passed"] is False
