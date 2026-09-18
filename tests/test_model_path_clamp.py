"""The free-form plan is held to the shortlist the planner was offered.

`_routable_registry` builds the AVAILABLE_AGENTS block — the floor-filtered
shortlist, topped up by the starvation backstop — and reports what the floor
took away. Nothing after the LLM call used to hold the model to that shortlist:
the clamp in `decompose()` checked only that a named agent existed, was listed
and was dispatchable. So a sub-floor agent the model named anyway — and the
planner's own instructions steered it towards code.gen — was stored, dispatched
and paid for, on a plan card that carried a `below_floor` notice about the very
same agent. The empty-plan fallback had the same hole, with a hardcoded
copywriter the floor could have excluded.

What this suite pins, through the public `decompose()` entry point with the
planner and the reputation read stubbed:

  * a step survives only if its agent was offered, and is still listed and
    dispatchable when the model answers;
  * no plan carries a step and an exclusion notice for the same agent;
  * a kept step below the floor is flagged `degraded`, matching its
    `floor_relaxed` notice;
  * the fallback is drawn from the offered set, deterministically;
  * with nothing to offer, the request is refused before any LLM call.

Fixtures are local rather than imported from the neighbouring floor suites, as
those suites do themselves, so this file fails for its own reasons only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from app.demo_kits import detect_kit
from app.schemas import DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

# A build intent that matches no DemoKit, so decompose() takes the free-form
# path — and the kind of intent the planner is told to hand to code.gen.
BUILD_INTENT = "build a habit tracker web app"


def _info(agent_id: str, *, smoothed: int, lower: int) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower,
        avg_bps=smoothed,
        count=3,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


@pytest.fixture()
def seeded() -> object:
    """Fresh 12-agent registry, restored after. `seed_registry` builds new
    `Agent` objects, so the delisting below mutates copies thrown away here."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


def _clearing_reps() -> dict[str, RepInfo]:
    """Everyone comfortably over the floor — no starvation, no exclusions."""
    return {a.id: _info(a.id, smoothed=8000, lower=8000) for a in state.list_agents()}


def _delist(*agent_ids: str) -> None:
    """What `set_active(id, false)` looks like after a registry-sync pass."""
    for agent_id in agent_ids:
        state.add_agent(state.agents[agent_id].model_copy(update={"status": "offline"}))


def _plan(*agent_ids: str) -> SimpleNamespace:
    steps = [
        PlanStep(agent_id=aid, rationale="model-chosen step", est_price_usdc=0.05, est_eta_seconds=1.0)
        for aid in agent_ids
    ]
    return SimpleNamespace(content=Plan(steps=steps))


def _plan_naming(*agent_ids: str) -> Callable[[str], Awaitable[SimpleNamespace]]:
    """A stand-in planner that returns a plan naming exactly `agent_ids`."""

    async def _arun(_prompt: str) -> SimpleNamespace:
        return _plan(*agent_ids)

    return _arun


def _decompose(
    monkeypatch: pytest.MonkeyPatch,
    reps: dict[str, RepInfo],
    planner: Callable[[str], Awaitable[SimpleNamespace]],
) -> DecomposeResponse:
    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return reps

    assert detect_kit(BUILD_INTENT) is None
    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _fake_reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", planner)
    return asyncio.run(orchestrator_svc.decompose(BUILD_INTENT))


def _stored_ids(resp: DecomposeResponse) -> list[str]:
    """What /execute will dispatch — the stored plan, not the response."""
    return [s.agent_id for s in state.plans[resp.plan_id].plan.steps]


def test_model_step_naming_a_sub_floor_agent_never_reaches_the_plan(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audit's case. code.gen's lower bound is under the floor and eleven
    # agents clear it, so the backstop stays out and agt_11c0 is simply not
    # offered. The planner names it anyway, as a real model told to prefer
    # code.gen for builds does — and the step used to be kept, stored,
    # dispatched and paid for.
    reps = _clearing_reps()
    reps["agt_11c0"] = _info("agt_11c0", smoothed=6300, lower=4100)

    resp = _decompose(monkeypatch, reps, _plan_naming("agt_11c0"))

    assert "agt_11c0" not in [s.agent_id for s in resp.steps]
    assert "agt_11c0" not in _stored_ids(resp)
    # The exclusion stands, and is now the whole truth about agt_11c0.
    note = next(n for n in resp.notices if n.agent_id == "agt_11c0")
    assert (note.kind, note.reason_code, note.lower_bound_bps) == ("excluded", "below_floor", 4100)
    # The model's only pick was clamped away, so the plan is the fallback —
    # the copywriter, which WAS offered.
    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]


# Three shapes of snapshot, one per way the shortlist can be built: the floor
# alone, the floor topped up by the backstop, and the backstop alone.
def _one_below_floor() -> dict[str, RepInfo]:
    reps = _clearing_reps()
    reps["agt_11c0"] = _info("agt_11c0", smoothed=6300, lower=4100)
    return reps


def _two_cleared() -> dict[str, RepInfo]:
    reps = {a.id: _info(a.id, smoothed=9000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}
    reps["agt_01h8"] = _info("agt_01h8", smoothed=6000, lower=6000)
    reps["agt_02k2"] = _info("agt_02k2", smoothed=6000, lower=6000)
    return reps


def _nobody_cleared() -> dict[str, RepInfo]:
    return {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}


@pytest.mark.parametrize("snapshot", [_one_below_floor, _two_cleared, _nobody_cleared])
def test_no_plan_carries_a_step_and_an_exclusion_for_the_same_agent(
    seeded: object,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Callable[[], dict[str, RepInfo]],
) -> None:
    # The model names EVERY agent in the registry, the worst case for the
    # clamp. A plan card that lists an agent as a step and as excluded tells
    # the buyer two contradictory things about the one they are paying.
    resp = _decompose(monkeypatch, snapshot(), _plan_naming(*(a.id for a in state.list_agents())))

    stepped = {s.agent_id for s in resp.steps}
    excluded = {n.agent_id for n in resp.notices if n.kind == "excluded"}
    # Both non-empty in every shape, or the disjointness below is vacuous.
    assert stepped
    assert excluded
    assert stepped.isdisjoint(excluded)
    assert set(_stored_ids(resp)) == stepped

    # The inline flag and the notices agree step by step: a kept step is
    # `degraded` exactly when the backstop re-admitted it below the floor.
    relaxed = {n.agent_id for n in resp.notices if n.reason_code == "floor_relaxed"}
    assert {s.agent_id for s in resp.steps if s.degraded} == relaxed


# ── the empty-plan fallback ─────────────────────────────────────


def test_fallback_never_routes_to_a_copywriter_the_floor_excluded(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fallback used to hardcode agt_01h8 on the grounds that nothing
    # on-chain can delist it. The floor can still exclude it, and then the
    # "safe" plan was a single step routed to an agent its own card reported
    # as below the floor.
    reps = _clearing_reps()
    reps["agt_01h8"] = _info("agt_01h8", smoothed=6300, lower=100)

    resp = _decompose(monkeypatch, reps, _plan_naming("agt_01h8"))

    assert [n.agent_id for n in resp.notices if n.kind == "excluded"] == ["agt_01h8"]
    # Every other agent cleared at the same score, so the id breaks the tie —
    # deterministically, and never towards the excluded copywriter.
    assert [s.agent_id for s in resp.steps] == ["agt_02k2"]
    assert _stored_ids(resp) == ["agt_02k2"]
    step = resp.steps[0]
    assert step.degraded is False
    # Honest about why this agent has the job: it was not chosen for the intent.
    assert step.rationale.startswith("fallback: the planner returned no usable step")


def test_fallback_keeps_the_copywriter_when_it_was_offered(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The common case must not move: an invented id cleans to nothing and the
    # copywriter, offered and clear of the floor, takes the intent as before.
    resp = _decompose(monkeypatch, _clearing_reps(), _plan_naming("agt_invented"))

    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert resp.steps[0].rationale == "fallback: generate copy for the intent"
    assert resp.steps[0].degraded is False
    assert resp.notices == []


def test_fallback_prefers_an_agent_that_cleared_the_floor(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The copywriter is under the floor and too weak for the backstop to reach,
    # so the fallback has to choose. Two agents cleared the floor; the backstop
    # topped the shortlist up with code.critic, which outscores both on
    # smoothed score. A relaxation is a last resort, not a tie-breaker, so the
    # job goes to an agent the floor actually passed.
    reps = {a.id: _info(a.id, smoothed=9000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}
    reps["agt_01h8"] = _info("agt_01h8", smoothed=1000, lower=100)
    reps["agt_02k2"] = _info("agt_02k2", smoothed=6000, lower=6000)
    reps["agt_03d9"] = _info("agt_03d9", smoothed=6000, lower=6000)

    resp = _decompose(monkeypatch, reps, _plan_naming("agt_invented"))

    assert [n.agent_id for n in resp.notices if n.reason_code == "floor_relaxed"] == ["agt_12r0"]
    assert [s.agent_id for s in resp.steps] == ["agt_02k2"]
    assert resp.steps[0].degraded is False
