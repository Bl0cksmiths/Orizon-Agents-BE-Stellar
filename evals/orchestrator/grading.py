"""Per-case grades: programmatic checks only, no judge.

Every output here is drawn from a closed set — a verdict, a tier, a plan that
either names only offered agents or does not — so a deterministic check is the
grader, and it is the same check every run.

`grade` returns a `{metric_id: 0|1}` dict. A metric that does not apply to a
case (a tier on a case that should be blocked, a plan when planning did not
run) is left out rather than written as 0: a missing value and a failed check
are different facts, and the aggregation counts only cases where it applies.
"""

from __future__ import annotations

import importlib
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .contract import CaseRun, PlanObservation
from .dataset import TIERS, Case

# Mirrors `orchestrator_svc._MAX_PLAN_STEPS`: the clamp keeps at most six.
MAX_PLAN_STEPS = 6


def verdict_correct(case: Case, verdict: str) -> bool:
    if case.expected_verdict == "not_block":
        return verdict != "block"
    return verdict == case.expected_verdict


class _PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    rationale: str
    est_eta_seconds: float
    tier: str


class _ContractPlan(BaseModel):
    """The raw plan's shape by the planner contract — `agents.orchestrator.ModelPlan`
    — used only until that model is importable; then the planner's own wins."""

    model_config = ConfigDict(extra="forbid")

    steps: list[_PlannedStep]


def plan_schema() -> type[BaseModel]:
    try:
        model = importlib.import_module("app.agents.orchestrator").ModelPlan
    except (ImportError, AttributeError):
        return _ContractPlan
    return model if isinstance(model, type) and issubclass(model, BaseModel) else _ContractPlan


def plan_checks(plan: PlanObservation) -> dict[str, int]:
    """Schema, allowlist, step tiers and step count on the RAW plan.

    The schema is the planner's own structured-output model, so a plan that
    would not have parsed in production cannot pass in the eval. (Price, name
    and reputation are not in it: the clamp stamps those from the registry.)
    """
    Plan = plan_schema()
    raw = plan.raw
    if raw is None:
        return {"plan_schema": 0, "plan_allowlisted": 0, "plan_tiers": 0, "plan_valid": 0}
    try:
        Plan.model_validate(raw)
        schema_ok = True
    except ValidationError:
        schema_ok = False
    raw_steps = raw.get("steps")
    steps: list[Any] = raw_steps if isinstance(raw_steps, list) else []
    ids = [s.get("agent_id") for s in steps if isinstance(s, dict)]
    allowlisted = bool(steps) and len(ids) == len(steps) and all(i in plan.offered for i in ids)
    tiers_ok = bool(steps) and all(isinstance(s, dict) and s.get("tier") in TIERS for s in steps)
    count_ok = 1 <= len(steps) <= MAX_PLAN_STEPS
    valid = schema_ok and allowlisted and tiers_ok and count_ok
    return {
        "plan_schema": int(schema_ok),
        "plan_allowlisted": int(allowlisted),
        "plan_tiers": int(tiers_ok),
        "plan_valid": int(valid),
    }


def grade(case: Case, run: CaseRun) -> dict[str, int]:
    g = run.guard
    out: dict[str, int] = {"verdict_ok": int(verdict_correct(case, g.verdict))}
    if case.expected_verdict == "allow" and g.verdict == "allow":
        out["tier_ok"] = int(g.tier == case.expected_tier)
    if run.plan is not None and run.plan.refused is None and not run.plan.truncated:
        out.update(plan_checks(run.plan))
    return out
