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
  * the fallback is drawn from the offered set, deterministically, and
    flagged `planner_fallback`, since the model did not choose it;
  * with nothing to offer, the request is refused before any LLM call.

Fixtures are local rather than imported from the neighbouring floor suites, as
those suites do themselves, so this file fails for its own reasons only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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
    # the copywriter, which WAS offered — and the response says it is one.
    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert resp.planner_fallback is True


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


def test_fallback_to_a_re_admitted_agent_is_flagged_degraded(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nobody clears the floor and the copywriter scores best, so the backstop
    # re-admits it and the fallback, preferring it, routes there. That is
    # allowed — it was offered — but it is a step below the floor, and the
    # inline flag has to say so as its `floor_relaxed` notice does.
    reps = _nobody_cleared()
    reps["agt_01h8"] = _info("agt_01h8", smoothed=5000, lower=100)

    resp = _decompose(monkeypatch, reps, _plan_naming("agt_invented"))

    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert resp.steps[0].degraded is True
    note = next(n for n in resp.notices if n.agent_id == "agt_01h8")
    assert (note.kind, note.reason_code) == ("degraded", "floor_relaxed")


# ── the registry moving while the planner runs ──────────────────


def test_agents_delisted_during_the_planning_call_are_clamped(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shortlist is built before the planning call, which can run for tens
    # of seconds. An operator who delists in that window still wins: code.gen
    # was offered and is what the model returns, but it and the copywriter are
    # withdrawn before the answer arrives — so the step is clamped and the
    # fallback passes over the copywriter it would otherwise prefer.
    async def _arun(_prompt: str) -> SimpleNamespace:
        _delist("agt_11c0", "agt_01h8")
        return _plan("agt_11c0")

    resp = _decompose(monkeypatch, _clearing_reps(), _arun)

    assert [s.agent_id for s in resp.steps] == ["agt_02k2"]
    assert _stored_ids(resp) == ["agt_02k2"]
    # The model answered, but with nothing still routable, so what is served
    # is the fallback and the response says so.
    assert resp.planner_fallback is True
    # A withdrawal is never a notice, however it arrives.
    assert resp.notices == []


def test_plan_is_refused_when_every_offered_agent_leaves_during_planning(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fallback may only route inside the offer, so when the whole offer is
    # withdrawn mid-call there is no honest plan left to return — and the old
    # hardcoded copywriter is exactly the dishonest one.
    async def _arun(_prompt: str) -> SimpleNamespace:
        _delist(*(a.id for a in state.list_agents()))
        return _plan("agt_11c0")

    before = set(state.plans)
    with pytest.raises(orchestrator_svc.NoRoutableAgentsError):
        _decompose(monkeypatch, _clearing_reps(), _arun)
    # Nothing was minted for /execute to find.
    assert set(state.plans) == before


def test_plan_is_refused_before_the_llm_when_nothing_can_be_offered(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every operator has withdrawn. The backstop may relax OUR floor but never
    # their delisting, so the shortlist is empty, and an empty AVAILABLE_AGENTS
    # block can only yield steps the clamp discards. Refused up front, so the
    # empty prompt never costs an LLM call or holds a planning slot.
    _delist(*(a.id for a in state.list_agents()))
    # Recorded, not raised: a planner call that raises is served the fallback
    # plan now, so a booby trap in here would be caught and the call it exists
    # to forbid would go unnoticed.
    calls: list[str] = []

    async def _arun(prompt: str) -> SimpleNamespace:
        calls.append(prompt)
        return _plan("agt_11c0")

    before = set(state.plans)
    with pytest.raises(orchestrator_svc.NoRoutableAgentsError):
        _decompose(monkeypatch, _clearing_reps(), _arun)
    assert set(state.plans) == before
    assert calls == []


def test_the_api_mints_no_plan_when_nothing_can_be_offered(
    seeded: object, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same refusal through the router the frontend calls. The request was
    # well-formed and the service cannot serve it, so the answer is a
    # server-side status and never a 200 carrying a plan: a retryable 503 with
    # its own detail, so a client can tell it apart from a hung planner (504)
    # or an unexpected fault (502). A planner that merely failed is neither: it
    # gets the fallback plan, flagged `planner_fallback` (BLO-121).
    #
    # The stand-in planner answers the way a model shown an empty list would:
    # with nothing usable. That is the answer the old hardcoded fallback turned
    # into a 200 plan routed to a withdrawn copywriter.
    calls: list[str] = []

    async def _arun(prompt: str) -> SimpleNamespace:
        calls.append(prompt)
        return _plan()

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _arun)
    # After the client starts: its lifespan re-seeds the catalog.
    _delist(*(a.id for a in state.list_agents()))
    before = set(state.plans)

    r = client.post("/api/orchestrator/decompose", json={"intent": BUILD_INTENT})

    assert r.status_code == 503
    assert r.json()["detail"] == "no_routable_agents"
    assert set(state.plans) == before
    assert calls == []


def test_the_model_cannot_write_its_own_reputation_onto_a_step(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # `PlanStep` is the planner's output schema, so every reputation field and
    # the `degraded` verdict are fields the model can fill in. The clamp
    # rebuilds each step from the registry and the snapshot, so whatever the
    # model wrote there is discarded rather than shown to the buyer.
    reps = _clearing_reps()

    async def _arun(_prompt: str) -> SimpleNamespace:
        forged = PlanStep(
            agent_id="agt_11c0",
            rationale="model-chosen step",
            est_price_usdc=0.0,
            est_eta_seconds=1.0,
            rep_bps=10_000,
            rep_source="onchain",
            rep_lower_bound_bps=10_000,
            rep_count=9_999,
            rep_dispute_rate_bps=0,
            rep_degraded=False,
            substituted_for="agt_03d9",
            degraded=True,
        )
        return SimpleNamespace(content=Plan(steps=[forged]))

    resp = _decompose(monkeypatch, reps, _arun)

    step = resp.steps[0]
    info = reps["agt_11c0"]
    assert (step.rep_bps, step.rep_lower_bound_bps, step.rep_count) == (
        info.smoothed_bps,
        info.lower_bound_bps,
        info.count,
    )
    assert step.est_price_usdc == state.agents["agt_11c0"].price
    assert step.substituted_for is None
    assert step.degraded is False
