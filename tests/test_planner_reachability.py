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
