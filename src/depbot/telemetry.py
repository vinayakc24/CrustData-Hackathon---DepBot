"""Structured run telemetry: one ``JobTelemetry`` document per repository job.

Built only from what the job actually recorded (state, command results, stage
spans). Written to ``<run>/telemetry.json`` and stored with the job in SQLite so
runs can be inspected and compared (``depbot runs show|compare``).
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from depbot import __version__ as DEPBOT_VERSION
from depbot.models import RepositoryUpgradeJob, RepositoryUpgradeOutcome, VerificationResult
from depbot.state import DepBotState

TELEMETRY_SCHEMA_VERSION = 1
PRICE_INPUT_ENV = "DEPBOT_LLM_USD_PER_MTOK_INPUT"
PRICE_OUTPUT_ENV = "DEPBOT_LLM_USD_PER_MTOK_OUTPUT"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageSpan(_Model):
    stage: str
    attempt: int  # job attempt (1-based) this span belongs to
    status: str  # ok | failed | blocked | cancelled | skipped
    started_at: str
    duration_seconds: float = Field(ge=0.0)
    detail: str | None = None


class LLMUsage(_Model):
    provider: str
    model: str | None = None
    calls: int = 0
    calls_by_task: dict[str, int] = Field(default_factory=dict)
    input_tokens: int | None = None  # None: the provider did not report usage
    output_tokens: int | None = None
    cost_usd: float | None = None  # None unless token counts and prices are both known


class PhaseResult(_Model):
    passed: bool
    failure_category: str | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    commands: dict[str, dict[str, Any]] = Field(default_factory=dict)


class JobTelemetry(_Model):
    schema_version: int = TELEMETRY_SCHEMA_VERSION
    fleet_run_id: str | None = None
    job_id: str
    run_id: str
    attempt: int = 1
    resumed_from: str | None = None  # checkpoint status a recovered job continued from
    repository_id: str
    repository_commit: str | None = None
    dependency: dict[str, str]
    toolchain: dict[str, str | None]
    execution: dict[str, str | None]
    stages: list[StageSpan] = Field(default_factory=list)
    repair_attempts: int = 0
    evidence: list[str] = Field(default_factory=list)
    patch_hashes: dict[str, str] = Field(default_factory=dict)
    final_diff_sha256: str | None = None
    verification: dict[str, PhaseResult] = Field(default_factory=dict)
    llm: LLMUsage | None = None
    job_status: str
    outcome: str
    reason: str | None = None
    duration_seconds: float = 0.0


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _version(args: list[str]) -> str | None:
    if shutil.which(args[0]) is None:
        return None
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15, shell=False,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    return lines[0].strip()[:200] if proc.returncode == 0 and lines else None


@lru_cache(maxsize=1)
def host_toolchain() -> dict[str, str | None]:
    """Tool versions on the DepBot host. With a container backend the build tools
    come from the image instead; ``execution.image_digest`` pins those."""
    return {
        "depbot": DEPBOT_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cmake": _version(["cmake", "--version"]),
        "cxx": _version([os.environ.get("CXX") or "c++", "--version"]),
        "conan": _version(["conan", "--version"]),
        "vcpkg": _version(["vcpkg", "version"]),
        "git": _version(["git", "--version"]),
    }


def llm_usage(client: object | None) -> LLMUsage | None:
    """Usage of the job's LLM client, read from what the client itself recorded."""
    if client is None:
        return None
    calls = list(getattr(client, "calls", []) or [])
    by_task: dict[str, int] = {}
    for task in calls:
        by_task[str(task)] = by_task.get(str(task), 0) + 1
    usage = getattr(client, "usage", None) or {}
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    cost = None
    try:
        if tokens_in is not None and tokens_out is not None and PRICE_INPUT_ENV in os.environ:
            cost = round(tokens_in / 1e6 * float(os.environ[PRICE_INPUT_ENV])
                         + tokens_out / 1e6 * float(os.environ.get(PRICE_OUTPUT_ENV, "0")), 6)
    except ValueError:
        cost = None
    return LLMUsage(
        provider=type(client).__name__, model=getattr(client, "model", None), calls=len(calls),
        calls_by_task=by_task, input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost,
    )


def _phase(result: VerificationResult) -> PhaseResult:
    commands = {}
    for name, step in (("install", result.dependency_install), ("configure", result.configure),
                       ("build", result.build), ("tests", result.tests)):
        if step is not None:
            commands[name] = {"exit_code": step.exit_code, "timed_out": step.timed_out,
                              "duration_seconds": step.duration_seconds}
    return PhaseResult(passed=result.passed, failure_category=result.failure_category,
                       tests_passed=result.tests_passed, tests_failed=result.tests_failed, commands=commands)


def build_job_telemetry(
    *,
    job: RepositoryUpgradeJob,
    state: DepBotState,
    outcome: RepositoryUpgradeOutcome,
    spans: list[StageSpan],
    execution: dict[str, str | None],
    llm_client: object | None,
    attempt: int = 1,
    resumed_from: str | None = None,
    fleet_run_id: str | None = None,
    final_diff: str | None = None,
) -> JobTelemetry:
    copy = state.repo_facts.get("working_copy") or {}
    evidence = sorted({url for change in state.breaking_changes for url in change.evidence_urls})
    verification = {
        phase: _phase(result)
        for phase, result in (("baseline", state.baseline), ("probe", state.probe), ("upgraded", state.verification))
        if result is not None
    }
    return JobTelemetry(
        fleet_run_id=fleet_run_id,
        job_id=job.job_id,
        run_id=state.run_id,
        attempt=attempt,
        resumed_from=resumed_from,
        repository_id=state.repository_id,
        repository_commit=copy.get("source_commit") if isinstance(copy, dict) else None,
        dependency={
            "name": state.dependency.name,
            "from_version": state.dependency.current_version,
            "to_version": state.dependency.target_version,
            "package_manager": state.dependency.package_manager.value,
        },
        toolchain=host_toolchain(),
        execution=execution,
        stages=spans,
        repair_attempts=state.repair_attempts,
        evidence=evidence,
        patch_hashes={p.patch_id: sha256_text(p.unified_diff) for p in state.patches},
        final_diff_sha256=sha256_text(final_diff) if final_diff is not None else None,
        verification=verification,
        llm=llm_usage(llm_client),
        job_status=state.status.value,
        outcome=outcome.status.value,
        reason=outcome.reason,
        duration_seconds=round(sum(span.duration_seconds for span in spans), 3),
    )
