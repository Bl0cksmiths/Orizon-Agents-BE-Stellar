"""Planner-safety filter for indexed agents (story 1.02) — an agent indexed
from the chain has no local worker until Epic 2 lands, so it must be
marketplace-visible but never planner-routable: excluded from the routing
prompt, clamped out of model-returned plans, and never admitted by the
floor-starvation fallback."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.registry import get_worker
from app.schemas import Agent, Plan, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

# The floor, delisting, binding and endpoint rules, on the routing policy they
# were written against (see the fixture).
pytestmark = pytest.mark.usefixtures("pre_pipeline_routing")


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


def _step(agent_id: str) -> PlanStep:
    return PlanStep(
        agent_id=agent_id,
        rationale="model-chosen step",
        est_price_usdc=0.05,
        est_eta_seconds=1.0,
    )


@pytest.fixture()
def indexed_agent():
    """A story-1.02 indexed agent: present in the registry, no local worker.
    (ext_ namespace on purpose — agt_ is the seeded catalog.) The whole
    registry is restored afterwards, not just the one agent added here."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    state.add_agent(
        Agent(
            id="ext_idx1",
            name="indexed.remote",
            skills=["remote"],
            price=0.02,
            rep=4.99,
            status="online",
            runs=0,
            real=False,
        )
    )
    yield
    state.agents.clear()
    state.agents.update(saved)


def test_prompt_fragment_excludes_workerless_agent(indexed_agent):
    # Everyone scores well above the floor; the indexed agent scores best of
    # all — reputation alone must not make it routable.
    reps = {a.id: _info(a.id, smoothed=8000, lower=8000) for a in state.list_agents()}
    reps["ext_idx1"] = _info("ext_idx1", smoothed=9999, lower=9999)

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    assert "id=ext_idx1 " not in fragment
    for a in state.list_agents():
        if a.id != "ext_idx1":
            assert f"id={a.id} " in fragment


def test_clamp_drops_model_step_for_workerless_agent(indexed_agent, monkeypatch):
    # The model names the indexed agent (it is in state.agents, so the
    # unknown-id clamp alone would keep it) alongside a real worker.
    async def arun_mixed(prompt):
        return SimpleNamespace(content=Plan(steps=[_step("ext_idx1"), _step("agt_11c0")]))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", arun_mixed)
    resp = asyncio.run(orchestrator_svc.decompose("write a haiku about databases"))

    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]
    stored = state.plans[resp.plan_id]
    assert [s.agent_id for s in stored.plan.steps] == ["agt_11c0"]

    # Model returns ONLY the workerless agent → cleaned is empty → the
    # fallback plan routes to copywrite, never a plan of /execute skips.
    async def arun_only_indexed(prompt):
        return SimpleNamespace(content=Plan(steps=[_step("ext_idx1")]))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", arun_only_indexed)
    resp = asyncio.run(orchestrator_svc.decompose("draft a landing page for a bakery"))

    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert [s.agent_id for s in state.plans[resp.plan_id].plan.steps] == ["agt_01h8"]


def test_kit_decompose_unaffected_by_indexed_agent(indexed_agent, client):
    r = client.post("/api/orchestrator/decompose", json={"intent": "tetris game in html"})
    assert r.status_code == 200
    steps = r.json()["steps"]
    assert steps
    for step in steps:
        assert step["agent_id"] != "ext_idx1"
        assert get_worker(step["agent_id"]) is not None


def test_floor_starvation_fallback_never_admits_workerless_agent(indexed_agent):
    # Every agent fails the floor; the indexed agent has the top smoothed
    # score and would head the fallback's top-3 sort without the filter.
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}
    reps["ext_idx1"] = _info("ext_idx1", smoothed=9999, lower=100)

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    listed = [ln for ln in fragment.splitlines() if ln.startswith("- id=")]
    assert len(listed) == 3
    assert "id=ext_idx1 " not in fragment
    executable = [a for a in state.list_agents() if get_worker(a.id) is not None]
    top3 = sorted(executable, key=lambda a: reps[a.id].smoothed_bps, reverse=True)[:3]
    for a in top3:
        assert f"id={a.id} " in fragment
