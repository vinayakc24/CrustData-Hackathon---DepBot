"""Classify observed verification failures without guessing a repair."""

from __future__ import annotations

import re

from depbot.models import VerificationResult


def _classify_text(text: str, *, stage: str) -> str:
    value = text.lower()
    if any(
        token in value
        for token in (
            "file not found", "no such file", "cannot find the file specified",
            "not recognized as", "winerror 2",
        )
    ):
        return "environment"
    if "timed out" in value:
        return "timeout"
    if any(token in value for token in ("no such file or directory", "cannot open include file", "cannot find include")):
        return "missing_header"
    if re.search(r"(?:undefined reference|unresolved external symbol|undefined symbol)", value):
        return "linker"
    if any(
        token in value
        for token in (
            "no member named", "was not declared", "has no member", "unknown type name",
            "is not a member of", "has not been declared",  # GCC/MSVC for a missing qualified name (e.g. toydep::old_sum)
            "identifier is undefined", "identifier \"", "undeclared identifier",
        )
    ):
        return "removed_symbol"
    if any(token in value for token in ("no matching function", "too few arguments", "too many arguments", "cannot convert argument")):
        return "signature_mismatch"
    if stage == "dependency_install":
        return "dependency_install"
    if stage == "configure":
        return "configuration"
    if stage == "build":
        return "compilation"
    if stage == "tests":
        return "test_regression"
    return "unknown"


def classify_failure(result: VerificationResult) -> str | None:
    """Return the first failing stage's category, using captured compiler output."""
    for step in (result.dependency_install, result.configure, result.build, result.tests):
        if step is None or step.succeeded:
            continue
        if step.timed_out:
            return "timeout"
        if step.exit_code is None:
            if "adapter install_args failed" in step.stderr_tail.lower():
                return "dependency_install"
            return "environment"
        # Structured diagnostics come from the full log; tails may cut off the error line.
        errors = "\n".join(d.message for d in result.diagnostics
                           if d.command_label == step.command_label and d.severity in ("error", "fatal error"))
        output = f"{errors}\n{step.stderr_tail}\n{step.stdout_tail}"
        return _classify_text(output, stage=step.command_label.rsplit(".", 1)[-1])
    if result.tests_failed:
        return "test_regression"
    return None
