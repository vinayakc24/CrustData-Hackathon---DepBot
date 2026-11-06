"""Policy gates. Every decision is recorded with its reasons (docs/plan.md §3.9)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from depbot.fleet.versions import change_level
from depbot.models import BreakingChange, ChangeLevel, CodeUsage, ImpactLevel, PlannedChange, UpgradePolicy


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: str  # "pre_migration", "plan_confidence", "pre_pr"
    allowed: bool
    policy_id: str
    change_level: ChangeLevel | None = None
    reasons: list[str] = Field(default_factory=list)


def pre_migration_gate(policy: UpgradePolicy, current: str, target: str) -> PolicyDecision:
    level = change_level(current, target)
    reasons: list[str] = []
    allowed = True
    if level == ChangeLevel.UNKNOWN:
        allowed = False
        reasons.append(f"cannot classify version change {current} -> {target}")
    elif level == ChangeLevel.NONE:
        allowed = False
        reasons.append(f"current {current} is already at or above target {target}")
    elif level not in policy.allowed_change_levels:
        allowed = False
        allowed_text = ", ".join(lv.value for lv in policy.allowed_change_levels)
        reasons.append(f"{level.value} upgrades are not allowed by policy (allowed: {allowed_text})")
    if (
        allowed
        and level == ChangeLevel.MAJOR
        and policy.require_human_approval_for_major
        and not policy.major_approved
    ):
        allowed = False
        reasons.append("major upgrade requires human approval (re-run with --approve-major)")
    if allowed:
        reasons.append(f"{level.value} upgrade {current} -> {target} permitted")
    return PolicyDecision(
        gate="pre_migration",
        allowed=allowed,
        policy_id=policy.policy_id,
        change_level=level,
        reasons=reasons,
    )


def plan_confidence_gate(
    policy: UpgradePolicy, changes: list[BreakingChange], plan: list[PlannedChange],
    usages: list[CodeUsage] | None = None,
) -> PolicyDecision:
    """Every change the migration relies on, and every planned edit, must meet the threshold.

    With ``usages``, a change is relied on when a planned edit or a definite/possible usage in
    this repository cites it. Low-confidence changes nothing here uses (a real release has many:
    unclassified changelog paragraphs, header-diff entries for macros the repo never touches)
    are recorded but do not block; the upgraded build and tests still have to pass.
    Without ``usages`` every change is checked (the strict, pre-0.9 behaviour).
    """
    threshold = policy.minimum_migration_confidence
    if usages is None:
        relied = {c.id for c in changes}
    else:
        relied = {cid for p in plan for cid in p.related_change_ids}
        relied |= {cid for u in usages if u.impact in (ImpactLevel.DEFINITE, ImpactLevel.POSSIBLE)
                   for cid in u.related_change_ids}
    low = [f"breaking change {c.id} confidence {c.confidence:.2f}" for c in changes
           if c.id in relied and c.confidence < threshold]
    low += [f"planned edit {p.file_path} confidence {p.confidence:.2f}" for p in plan if p.confidence < threshold]
    unused = sorted(c.id for c in changes if c.id not in relied and c.confidence < threshold)
    reasons = [f"below minimum {threshold:.2f}: {item}" for item in low] or [
        f"all relied-on changes and planned edits meet confidence >= {threshold:.2f}"
    ]
    if unused:
        reasons.append(f"not used by this repository, not gating ({len(unused)} low-confidence): {', '.join(unused)}")
    return PolicyDecision(
        gate="plan_confidence", allowed=not low, policy_id=policy.policy_id, reasons=reasons
    )


def pre_pr_gate(policy: UpgradePolicy, verification_passed: bool, tests_ran: bool) -> PolicyDecision:
    reasons: list[str] = []
    allowed = True
    if not verification_passed and not policy.create_pr_on_failure:
        allowed = False
        reasons.append("verification did not pass; policy forbids PRs on failure")
    if policy.require_tests and not tests_ran:
        allowed = False
        reasons.append("policy requires tests but no test command ran")
    reasons.append("auto-merge is disabled")
    return PolicyDecision(gate="pre_pr", allowed=allowed, policy_id=policy.policy_id, reasons=reasons)
