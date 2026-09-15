"""A bound external agent is ROUTABLE, an unbound one still is not (story 2.01).

Story 1.02 indexed on-chain agents into the marketplace with no local worker,
and the planner was taught to ignore them (tests/test_planner_routability.py).
Story 2.01 gives an operator a durable way to attach an endpoint to one — at
which point the 1.02 rule is too broad: a bound agent has something to execute
a step with, so the planner must be allowed to pick it. These tests pin the new
line (`binding_registry.is_dispatchable`) on both sides, and pin the thing the
widening must NOT change: the reputation floor still judges a bound external
agent by exactly the arithmetic a local one is judged by.
"""

from __future__ import annotations

import asyncio

import pytest

from app.schemas import Agent
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

BOUND = "ext_bound1"
UNBOUND = "ext_unbound1"


class _FakeStore:
    """Stands in for a BindingStore. Only `list_agent_ids` is exercised —
    `refresh_bound_ids` is the one public way to load the routable set."""

    def __init__(self, *agent_ids: str) -> None:
        self._ids = frozenset(agent_ids)

    async def list_agent_ids(self) -> frozenset[str]:
        return self._ids


def _load_bound(monkeypatch, *agent_ids: str) -> None:
    """Seed (or clear) the bound set through the registry's public surface."""
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _FakeStore(*agent_ids))
    asyncio.run(binding_registry.refresh_bound_ids())


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


def _external(agent_id: str) -> Agent:
    """An indexed on-chain agent: in the marketplace, no local worker.
    Skills overlap the seeded catalog so it is a plausible plan step."""
    return Agent(
        id=agent_id,
        name=f"indexed.{agent_id}",
        skills=["code", "html"],
        price=0.02,
        rep=4.99,
        status="online",
        runs=0,
        real=False,
    )


@pytest.fixture()
def external_agents(monkeypatch):
    """Two workerless indexed agents — one bound, one not — and a bound set
    that is emptied again afterwards so nothing leaks into the next test."""
    seed_registry()
    state.add_agent(_external(BOUND))
    state.add_agent(_external(UNBOUND))
    _load_bound(monkeypatch, BOUND)
    yield
    state.agents.pop(BOUND, None)
    state.agents.pop(UNBOUND, None)
    _load_bound(monkeypatch)


def test_bound_external_agent_is_offered_to_the_planner(external_agents):
    # Everyone clears the floor, so no starvation fallback is involved: this is
    # the plain routable set.
    reps = {a.id: _info(a.id, smoothed=8000, lower=8000) for a in state.list_agents()}

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    assert f"id={BOUND} " in fragment
    assert f"id={UNBOUND} " not in fragment


def test_binding_does_not_exempt_an_external_agent_from_the_floor(external_agents):
    # The bound agent scores best of anyone on smoothed score but its lower
    # bound is under the floor — the same shape that excludes a local agent.
    # Plenty of others clear the floor, so the starvation fallback stays out.
    reps = {a.id: _info(a.id, smoothed=8000, lower=8000) for a in state.list_agents()}
    reps[BOUND] = _info(BOUND, smoothed=9999, lower=0)

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    assert f"id={BOUND} " not in fragment


def test_floor_starvation_fallback_admits_a_bound_agent_but_never_an_unbound_one(external_agents):
    # Nobody clears the floor, so the fallback re-admits the top 3 by smoothed
    # score. The two workerless agents hold the top two scores; only the bound
    # one may be re-admitted, and the unbound one may not take a slot from a
    # local agent.
    agents = state.list_agents()
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=0) for i, a in enumerate(agents)}
    reps[BOUND] = _info(BOUND, smoothed=9999, lower=0)
    reps[UNBOUND] = _info(UNBOUND, smoothed=9998, lower=0)

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    listed = [ln for ln in fragment.splitlines() if ln.startswith("- id=")]
    assert len(listed) == 3
    assert f"id={BOUND} " in fragment
    assert f"id={UNBOUND} " not in fragment
