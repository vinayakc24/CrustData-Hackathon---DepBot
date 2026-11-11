"""`depbot run --workers/--exec-*` and `depbot runs list|show|compare|cancel|resume`."""

import json

import pytest

import depbot.cli as cli
from depbot.cli import main
from depbot.persistence.store import Store
from tests.integration.test_durable_fleet import Clock, Crash, fleet_dir  # noqa: F401  (fixture)
from tests.unit.test_orchestrator import _stages


@pytest.fixture
def stages(monkeypatch):
    holder = {"stages": _stages()}
    monkeypatch.setattr(cli, "load_default_stages", lambda: holder["stages"])
    return holder


def _base(fleet_dir):  # noqa: F811
    return ["--fleet", str(fleet_dir / "fleet.yaml")]


def _run(fleet_dir, *extra):  # noqa: F811
    return main(["run", *_base(fleet_dir), "--event", str(fleet_dir / "event.yaml"),
                 "--artifacts", str(fleet_dir / "artifacts"), *extra])


def _json(capsys):
    return json.loads(capsys.readouterr().out)


def test_run_then_list_show_and_job_detail(fleet_dir, stages, capsys):  # noqa: F811
    assert _run(fleet_dir, "--workers", "2", "--exec-cpus", "0.1", "--exec-memory-mb", "64") == 0
    capsys.readouterr()
    assert main(["runs", "list", *_base(fleet_dir), "--json"]) == 0
    (row,) = _json(capsys)
    assert row["status"] == "completed" and row["jobs"] == {"completed": 3} and row["outcomes"] == {"verified": 3}

    assert main(["runs", "show", row["fleet_run_id"][:20], *_base(fleet_dir), "--json"]) == 0
    shown = _json(capsys)
    assert [j["repository_id"] for j in shown["jobs"]] == ["repo_a", "repo_b", "repo_c"]
    assert shown["jobs"][0]["stages"][0] == {"stage": "inspect", "attempt": 1, "status": "ok",
                                             "duration_seconds": shown["jobs"][0]["stages"][0]["duration_seconds"],
                                             "detail": None}
    assert "telemetry" not in shown["jobs"][0]

    assert main(["runs", "show", row["fleet_run_id"], "--job", "repo_b", *_base(fleet_dir), "--json"]) == 0
    (job,) = _json(capsys)["jobs"]
    telemetry = job["telemetry"]
    assert telemetry["outcome"] == "verified" and telemetry["job_status"] == "VERIFIED"
    assert telemetry["toolchain"]["python"] and "cmake" in telemetry["toolchain"]
    assert telemetry["execution"] == {"backend": "local", "image": None, "image_digest": None}
    assert telemetry["evidence"] == ["file://CHANGELOG.md"]
    assert list(telemetry["patch_hashes"]) == ["p1"] and len(telemetry["final_diff_sha256"]) == 64
    assert telemetry["verification"]["upgraded"]["passed"] is True
    assert telemetry["verification"]["upgraded"]["tests_passed"] == 1
    assert telemetry["llm"] is None and telemetry["repair_attempts"] == 0
    assert [t["to"] for t in job["transitions"]][-1] == "VERIFIED"
    assert {a["kind"] for a in job["artifacts"]} >= {"state", "telemetry", "patch"}

    assert main(["runs", "show", row["fleet_run_id"], *_base(fleet_dir)]) == 0
    assert "repo_a" in capsys.readouterr().out


def test_compare_shows_outcome_and_patch_differences_between_runs(fleet_dir, stages, capsys):  # noqa: F811
    assert _run(fleet_dir) == 0

    def failing_verify(phase, **kw):
        result = _stages().verify(phase, **kw)
        if phase == "upgraded":
            bad = result.build.model_copy(update={"exit_code": 2})
            return result.model_copy(update={"build": bad, "passed": False})
        return result

    stages["stages"] = _stages(verify=failing_verify, repair=None)
    assert _run(fleet_dir, "--force") == 1
    capsys.readouterr()
    assert main(["runs", "list", *_base(fleet_dir), "--json"]) == 0
    first, second = (r["fleet_run_id"] for r in _json(capsys))
    assert main(["runs", "compare", first, second, *_base(fleet_dir), "--json"]) == 0
    diff = _json(capsys)
    assert diff["outcome_changes"] == 3
    repo = diff["repositories"][0]
    assert repo["outcome"] == {"left": "verified", "right": "failed", "changed": True}
    assert repo["final_diff_identical"] is True and "verify" in repo["stage_duration_delta"]
    assert main(["runs", "compare", first, second, *_base(fleet_dir)]) == 0
    assert "CHANGED" in capsys.readouterr().out


def test_cancel_and_resume_commands(fleet_dir, stages, capsys, monkeypatch):  # noqa: F811
    crashed = []

    def impact(**kw):
        if not crashed and "repo_a" in str(kw["worktree"]):
            crashed.append(1)
            raise Crash("worker died")
        return _stages().impact(**kw)

    stages["stages"] = _stages(impact=impact)
    assert _run(fleet_dir) == 1
    capsys.readouterr()
    store = Store.for_state_dir(fleet_dir / ".depbot")
    run_id = store.fleet_runs()[0]["fleet_run_id"]
    assert _run(fleet_dir) == 3  # duplicate event: told to resume instead
    assert "runs resume" in capsys.readouterr().err

    assert main(["runs", "resume", run_id, *_base(fleet_dir)]) == 1  # lease still held: nothing taken over
    capsys.readouterr()
    with store.transaction() as db:  # the dead worker's lease runs out
        db.execute("UPDATE jobs SET lease_expires=0 WHERE status='running'")
    assert main(["runs", "resume", run_id, *_base(fleet_dir), "--json"]) == 0
    assert {o["status"] for o in _json(capsys)["outcomes"]} == {"verified"}

    assert main(["runs", "cancel", run_id, *_base(fleet_dir)]) == 0
    assert "cancel requested" in capsys.readouterr().out
    assert main(["runs", "resume", run_id, *_base(fleet_dir)]) == 2
    assert "was cancelled" in capsys.readouterr().err


def test_unusable_or_unsafe_execution_settings_fail_before_any_job(fleet_dir, stages, capsys):  # noqa: F811
    assert _run(fleet_dir, "--exec-backend", "docker") == 2  # no image
    assert "requires an image" in capsys.readouterr().err
    assert _run(fleet_dir, "--exec-backend", "docker", "--exec-image", "img", "--exec-mount-rw", "/var/run/docker.sock") == 2
    assert "refusing to mount" in capsys.readouterr().err
    assert not (fleet_dir / "artifacts").exists() and not (fleet_dir / ".depbot" / "depbot.db").exists()
    assert _run(fleet_dir) == 0  # the event was not consumed by the refused attempts


def test_runs_commands_explain_missing_history(fleet_dir, capsys):  # noqa: F811
    assert main(["runs", "list", *_base(fleet_dir)]) == 2
    assert "no run history" in capsys.readouterr().err
    assert not (fleet_dir / ".depbot" / "depbot.db").exists()
