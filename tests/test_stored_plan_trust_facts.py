"""A stored plan keeps the trust facts the buyer was shown.

`DecomposeResponse` tells the buyer four things about how a plan was built —
the floor notices, the floor it was judged against, whether reputation was
estimated, and whether the planner fell back — and `StoredPlan` used to drop
all four. `/execute` and any later read then could not tell a plan judged on
priors, served as a fallback or built with the floor relaxed from one that
was not. These pin that the facts survive into and out of the stored shape.
"""

from __future__ import annotations

from app.schemas import Agent, DecomposeResponse, Plan, PlanStep, StoredPlan
from app.services import plan_notices, reputation_svc
from app.state import state

TRUST_FACTS = ("notices", "floor_bps", "reputation_degraded", "planner_fallback")


def _notices():
    agent = Agent(id="ext_low", name="low", skills=["x"], price=0.02, rep=0.0, status="online", runs=0)
    info = reputation_svc._prior_info("ext_low").model_copy(update={"source": "onchain", "lower_bound_bps": 4200})
    return [
        plan_notices.below_floor_exclusion(agent, info),
        plan_notices.relaxation(agent, info, min_routable=3),
    ]


def _step() -> PlanStep:
    return PlanStep(
        agent_id="ext_low", agent_name="low", rationale="r", est_price_usdc=0.02, est_eta_seconds=1.0, degraded=True
    )


def _plan(plan_id: str) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent="ship it",
        plan=Plan(steps=[_step()]),
        total_usdc=0.02,
        total_eta=1.0,
        notices=_notices(),
        floor_bps=5500,
        reputation_degraded=True,
        planner_fallback=True,
    )


def test_every_trust_fact_survives_a_serialisation_round_trip():
    plan = _plan("pln_trust_json")
    back = StoredPlan.model_validate_json(plan.model_dump_json())
    assert back == plan
    assert [n.reason_code for n in back.notices] == ["below_floor", "floor_relaxed"]
    assert back.floor_bps == 5500
    assert back.reputation_degraded is True
    assert back.planner_fallback is True
    assert back.created_at == plan.created_at


def test_every_trust_fact_survives_the_plan_store():
    plan = _plan("pln_trust_store")
    state.add_plan(plan)
    try:
        stored = state.plans[plan.id]
    finally:
        state.plans.pop(plan.id, None)
    for field in TRUST_FACTS:
        assert getattr(stored, field) == getattr(plan, field), field


def test_the_trust_facts_carry_over_from_a_decompose_response_by_name():
    """The stored names are the response's names, so whoever builds the stored
    plan copies the four facts straight across — no renaming to get wrong."""
    resp = DecomposeResponse(
        plan_id="pln_trust_resp",
        intent="ship it",
        steps=[_step()],
        total_usdc=0.02,
        total_eta=1.0,
        notices=_notices(),
        floor_bps=5500,
        reputation_degraded=True,
        planner_fallback=True,
    )
    stored = StoredPlan(
        id=resp.plan_id,
        intent=resp.intent,
        plan=Plan(steps=resp.steps),
        total_usdc=resp.total_usdc,
        total_eta=resp.total_eta,
        **{field: getattr(resp, field) for field in TRUST_FACTS},
    )
    back = StoredPlan.model_validate_json(stored.model_dump_json())
    for field in TRUST_FACTS:
        assert getattr(back, field) == getattr(resp, field), field


def test_a_plan_stored_without_them_reports_nothing_rather_than_a_verdict():
    """The defaults are "nothing to report": no notices, no flags, and no floor
    — not a floor of zero, which would read as a verdict."""
    plan = StoredPlan(id="pln_trust_bare", intent="i", plan=Plan(steps=[]), total_usdc=0.0, total_eta=0.0)
    assert plan.notices == []
    assert plan.floor_bps is None
    assert plan.reputation_degraded is False
    assert plan.planner_fallback is False
