"""Both plan paths keep what the buyer was shown on the plan they store.

`StoredPlan` carries the plan-level trust facts — the floor notices, the floor,
whether reputation was read on estimates, and whether the planner fell back —
so `/execute` judges the plan the buyer actually authorised. The fields have
"nothing to report" defaults, so a constructor that forgets one still
validates; these tests are what notice the omission. Each run is arranged so
every fact holds a NON-default value, which a missing argument cannot fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import settings
from app.schemas import Agent, Plan, PlanStep
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc, reputation_svc
from app.services.reputation_svc import RepInfo
from app.state import state
from app.stellar import cache as rcache

FREE_FORM_INTENT = "write a haiku about databases"
KIT_INTENT = "tetris game in html"
BAD = "agt_04m1"
UNREAD = "agt_12r0"


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(binding_registry, "_bound_ids", set())
    rcache.clear()
    yield
    state.agents.clear()
    state.agents.update(saved)
    rcache.clear()


def _reps(ids: list[str]) -> dict[str, RepInfo]:
    """Every agent on the prior, except one far below the floor and one unread."""
    out = {i: reputation_svc._prior_info(i) for i in ids}
    if BAD in out:
        out[BAD] = out[BAD].model_copy(
            update={"source": "onchain", "count": 40, "smoothed_bps": 1_000, "lower_bound_bps": 1_000}
        )
    if UNREAD in out:
        out[UNREAD] = out[UNREAD].model_copy(update={"degraded": True})
    return out


def _install_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_reps(ids: list[str], *_a: object, **_k: object) -> dict[str, RepInfo]:
        return _reps(list(ids))

    monkeypatch.setattr(reputation_svc, "fetch_reps", _fake_reps)


def _planner(*agent_ids: str) -> Any:
    async def _arun(_prompt: str) -> Any:
        steps = [PlanStep(agent_id=a, rationale="r", est_price_usdc=0.0, est_eta_seconds=1.0) for a in agent_ids]
        return type("Run", (), {"content": Plan(steps=steps)})()

    return _arun


def test_the_free_form_plan_stores_the_facts_the_buyer_was_shown(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_reads(monkeypatch)
    # A planner that names nothing it may route forces the fallback plan.
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _planner("agt_nobody"))

    resp = asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))
    stored = state.plans[resp.plan_id]

    assert resp.notices, "the run must produce a notice, or the notices check proves nothing"
    assert stored.notices == resp.notices
    assert stored.floor_bps == settings.reputation_floor_bps == resp.floor_bps
    assert resp.reputation_degraded is True
    assert stored.reputation_degraded is True
    assert resp.planner_fallback is True
    assert stored.planner_fallback is True


def test_the_kit_plan_stores_the_facts_the_buyer_was_shown(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_reads(monkeypatch)

    async def _no_pause() -> None:
        return None

    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_pause)
    # One unbound external registration, so the kit plan reports a notice.
    state.add_agent(
        Agent(
            id="ext_unbound",
            name="unbound",
            skills=["x"],
            price=0.01,
            rep=5.0,
            status="online",
            runs=0,
            source="onchain",
        )
    )

    resp = asyncio.run(orchestrator_svc.decompose(KIT_INTENT))
    stored = state.plans[resp.plan_id]

    assert resp.notices, "the run must produce a notice, or the notices check proves nothing"
    assert stored.notices == resp.notices
    assert stored.floor_bps == settings.reputation_floor_bps == resp.floor_bps
    assert resp.reputation_degraded is True
    assert stored.reputation_degraded is True
    # The kit is not the planner, so it never reports a planner fallback.
    assert stored.planner_fallback is False
