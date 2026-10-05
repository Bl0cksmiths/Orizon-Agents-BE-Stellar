"""D-084: the planner must not route to an endpoint its own probe found dead.

UAT reproduced it on the live deploy: `POST /api/orchestrator/decompose`
routed its one step to `algorex` with no notice, while
`GET /api/agents/algorex/readiness` reported `reachable: failed` ("answered
301, a redirect. Dispatches never follow redirects."). The buyer signed an
authorize, the step failed in 0.3 s, and the buyer paid two transactions' fees
for a run the platform already knew could not be delivered.

Pinned here:

  * every readiness probe feeds the planner's reachability memory;
  * an agent with a fresh failed probe is left out of the plan on BOTH paths,
    with an `unreachable_endpoint` notice beside the existing `below_floor` and
    `unbound_endpoint` ones — and is never named as unbound;
  * the exclusion lapses with the freshness window, so a blip is not forever;
  * a probe that lands while the planner is thinking still keeps the step out;
  * the planner re-probes bound agents it has no fresh verdict on, in the
    background, so the memory does not depend on anyone running the check.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc, reachability
from app.services import operator_readiness as readiness
from app.services.binding_store import BindingRecord
from app.services.reputation_svc import RepInfo
from app.state import state
from app.stellar import cache as rcache

DEAD = "ext_dead"
ALIVE = "ext_alive"
FREE_FORM_INTENT = "write a launch announcement for a neighbourhood bakery"
KIT_INTENT = "tetris game in html"


def _rep(agent_id: str) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=8000,
        lower_bound_bps=8000,
        avg_bps=8000,
        count=5,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


def _external(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        name=f"{agent_id}.remote",
        skills=["copy", "seo"],
        price=0.02,
        rep=4.9,
        status="online",
        runs=0,
        source="onchain",
    )


class _FakeStore:
    def __init__(self, *agent_ids: str) -> None:
        self._ids = frozenset(agent_ids)

    async def list_agent_ids(self) -> frozenset[str]:
        return self._ids


async def _noop(*_a: object, **_k: object) -> None:
    return None


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch):
    """The seeded catalog plus two bound external agents, every one scored above
    the floor, a clean reachability memory and no background probing unless a
    test asks for it."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    for agent_id in (DEAD, ALIVE):
        state.add_agent(_external(agent_id))
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _FakeStore(DEAD, ALIVE))
    asyncio.run(binding_registry.refresh_bound_ids())
    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _noop)

    async def _reps(ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return {a.id: _rep(a.id) for a in state.list_agents()}

    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _reps)
    reachability.reset()
    yield SimpleNamespace()
    reachability.reset()
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _FakeStore())
    asyncio.run(binding_registry.refresh_bound_ids())
    state.agents.clear()
    state.agents.update(saved)


def _free_form(monkeypatch: pytest.MonkeyPatch, picks: list[str], during=None) -> DecomposeResponse:
    async def _arun(*_a: object, **_k: object) -> SimpleNamespace:
        if during is not None:
            during()
        steps = [
            PlanStep(agent_id=a, rationale=f"step for {a}", est_price_usdc=0.05, est_eta_seconds=1.0) for a in picks
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _arun)
    return asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))


def _codes(resp: DecomposeResponse) -> dict[str, str]:
    return {n.agent_id: n.reason_code for n in resp.notices}


# ── readiness feeds the memory ──────────────────────────────────


def _endpoint(probe: readiness.ProbeResult | None) -> readiness._Endpoint:
    record = BindingRecord(
        agent_id=DEAD,
        endpoint_url="https://dead.example/run",
        owner="G" + "A" * 55,
        bound_at=0.0,
        previous_endpoint_url=None,
    )
    return readiness._Endpoint(readiness._Read(record), probe)


def _run_readiness(monkeypatch: pytest.MonkeyPatch, probe: readiness.ProbeResult | None) -> readiness.Readiness:
    async def _check(_agent_id: str) -> readiness._Endpoint:
        return _endpoint(probe)

    async def _no_owner(_agent_id: str) -> None:
        return None

    rcache.clear()
    monkeypatch.setattr(readiness, "_check_endpoint", _check)
    monkeypatch.setattr(readiness.external_binding, "resolve_owner", _no_owner)
    return asyncio.run(readiness.check_readiness(DEAD))


def test_a_failed_readiness_probe_is_remembered_for_the_planner(world, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_readiness(monkeypatch, readiness.ProbeResult("http_status", status_code=301))

    assert {s.key: s.status for s in result.steps}["reachable"] == "failed"
    assert reachability.is_failing(DEAD)


def test_a_passing_readiness_probe_clears_the_memory(world, monkeypatch: pytest.MonkeyPatch) -> None:
    reachability.record(DEAD, "failed")

    _run_readiness(monkeypatch, readiness.ProbeResult("ok", status_code=200))

    assert not reachability.is_failing(DEAD)


def test_a_probe_that_could_not_run_is_not_a_verdict(world, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_readiness(monkeypatch, None)

    assert not reachability.is_failing(DEAD)
    assert not reachability.has_fresh_verdict(DEAD)


# ── the planner leaves a dead endpoint out ──────────────────────


def test_the_free_form_planner_never_routes_to_a_fresh_failure(world, monkeypatch: pytest.MonkeyPatch) -> None:
    # The model names the dead agent anyway — it knows it from an earlier turn
    # or just invents the choice. The clamp holds it to the offered set.
    reachability.record(DEAD, "failed")

    resp = _free_form(monkeypatch, [DEAD, ALIVE])

    assert [s.agent_id for s in resp.steps] == [ALIVE]
    assert _codes(resp)[DEAD] == "unreachable_endpoint"
    stored = state.plans.get(resp.plan_id)
    assert stored is not None and [n.reason_code for n in stored.notices] == [n.reason_code for n in resp.notices]


def test_a_dead_endpoint_is_never_offered_to_the_planner(world) -> None:
    # Not merely clamped after the fact: an agent the model is shown is one it
    # will plan around, and a plan built around a step that is then dropped
    # is a worse plan than one built without it.
    reachability.record(DEAD, "failed")
    reps = {a.id: _rep(a.id) for a in state.list_agents()}

    shortlist = orchestrator_svc._routable_registry(reps)

    assert DEAD not in shortlist.offered
    assert f"id={DEAD} " not in shortlist.block
    assert ALIVE in shortlist.offered


def test_a_dead_endpoint_is_never_called_unbound(world, monkeypatch: pytest.MonkeyPatch) -> None:
    # It is bound: "no endpoint bound" would send its operator to fix the
    # wrong thing. One notice, with the right code.
    reachability.record(DEAD, "failed")

    resp = _free_form(monkeypatch, [ALIVE])

    assert [n.reason_code for n in resp.notices if n.agent_id == DEAD] == ["unreachable_endpoint"]


def test_the_kit_path_reports_it_the_same_way(world, monkeypatch: pytest.MonkeyPatch) -> None:
    reachability.record(DEAD, "failed")

    async def _no_llm(*_a: object, **_k: object) -> None:
        raise AssertionError("the kit path must never call the LLM")

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _no_llm)
    kit = asyncio.run(orchestrator_svc.decompose(KIT_INTENT))
    free_form = _free_form(monkeypatch, [ALIVE])

    def _unreachable(resp: DecomposeResponse) -> list[tuple[str, str, str, int | None]]:
        return [
            (n.kind, n.agent_id, n.reason, n.floor_bps) for n in resp.notices if n.reason_code == "unreachable_endpoint"
        ]

    assert (
        _unreachable(kit)
        == _unreachable(free_form)
        == [("excluded", DEAD, _unreachable(kit)[0][2], settings.reputation_floor_bps)]
    )
    assert DEAD not in {s.agent_id for s in kit.steps}


def test_the_exclusion_lapses_with_the_freshness_window(world, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 5_000.0}
    monkeypatch.setattr(reachability, "_now", lambda: clock["now"])
    reachability.record(DEAD, "failed")
    clock["now"] += reachability.FAILURE_FRESH_SECONDS + 1

    resp = _free_form(monkeypatch, [DEAD])

    assert [s.agent_id for s in resp.steps] == [DEAD]
    assert DEAD not in _codes(resp)


def test_a_failure_that_lands_while_the_planner_thinks_still_keeps_the_step_out(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offered (no verdict yet), then the background probe lands during the
    # planning call: the clamp asks again at the point of use.
    resp = _free_form(monkeypatch, [DEAD, ALIVE], during=lambda: reachability.record(DEAD, "failed"))

    assert [s.agent_id for s in resp.steps] == [ALIVE]
    assert _codes(resp)[DEAD] == "unreachable_endpoint"


def test_the_fallback_never_lands_on_the_dead_agent(world, monkeypatch: pytest.MonkeyPatch) -> None:
    # The model picks only the dead agent, so the clamp empties the plan:
    # the fallback still draws from the offered set, never from the dead one.
    reachability.record(DEAD, "failed")

    resp = _free_form(monkeypatch, [DEAD])

    assert DEAD not in {s.agent_id for s in resp.steps}
    assert resp.planner_fallback


# ── the planner keeps the memory warm ───────────────────────────


@pytest.fixture()
def probes(monkeypatch: pytest.MonkeyPatch):
    """Replace the endpoint check under `refresh_stale` with a scripted one."""
    script: dict[str, readiness.ProbeResult | None] = {}
    calls: list[str] = []

    async def _check(agent_id: str) -> readiness._Endpoint:
        calls.append(agent_id)
        await asyncio.sleep(0)
        return _endpoint(script.get(agent_id))

    monkeypatch.setattr(readiness, "_check_endpoint", _check)
    reachability.reset()
    yield SimpleNamespace(script=script, calls=calls)
    reachability.reset()


async def _drain() -> None:
    while reachability.in_flight():
        await asyncio.sleep(0)


def test_refresh_probes_an_unknown_agent_and_remembers_the_verdict(probes) -> None:
    probes.script[DEAD] = readiness.ProbeResult("http_status", status_code=301)
    probes.script[ALIVE] = readiness.ProbeResult("ok", status_code=200)

    async def _go() -> None:
        reachability.refresh_stale([DEAD, ALIVE])
        await _drain()

    asyncio.run(_go())

    assert sorted(probes.calls) == [ALIVE, DEAD]
    assert reachability.is_failing(DEAD)
    assert not reachability.is_failing(ALIVE) and reachability.has_fresh_verdict(ALIVE)


def test_refresh_skips_fresh_verdicts_and_never_doubles_a_probe(probes) -> None:
    reachability.record(ALIVE, "done")

    async def _go() -> None:
        reachability.refresh_stale([DEAD, ALIVE])
        reachability.refresh_stale([DEAD])
        await _drain()

    asyncio.run(_go())

    assert probes.calls == [DEAD]


def test_refresh_is_bounded(probes) -> None:
    ids = [f"ext_{i}" for i in range(reachability.MAX_IN_FLIGHT * 3)]

    async def _go() -> int:
        reachability.refresh_stale(ids)
        started = reachability.in_flight()
        await _drain()
        return started

    assert asyncio.run(_go()) == reachability.MAX_IN_FLIGHT


def test_refresh_outside_an_event_loop_is_a_no_op(probes) -> None:
    reachability.refresh_stale([DEAD])

    assert probes.calls == []


def test_refresh_survives_a_probe_that_raises(probes, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_agent_id: str) -> readiness._Endpoint:
        raise RuntimeError("binding store down")

    monkeypatch.setattr(readiness, "_check_endpoint", _boom)

    async def _go() -> None:
        reachability.refresh_stale([DEAD])
        await _drain()

    asyncio.run(_go())

    assert not reachability.has_fresh_verdict(DEAD)


def test_decompose_asks_after_the_bound_agents_it_could_route_to(world, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only external agents: a seeded agent runs on a local worker, so there is
    # no endpoint to probe. A fresh failure is not re-asked until it lapses.
    asked: list[str] = []
    monkeypatch.setattr(reachability, "refresh_stale", lambda ids: asked.extend(ids))
    reachability.record(DEAD, "failed")

    _free_form(monkeypatch, [ALIVE])

    assert asked == [ALIVE]
