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
