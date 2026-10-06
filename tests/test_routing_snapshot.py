"""One registry snapshot per decompose, and reputation read only for what it can route.

`decompose` used to read reputation for EVERY registry entry. The registry is
permissionless — `registry_sync` indexes every on-chain registration with no
cap — and unbound or delisted agents are unroutable by definition, so each spam
registration added a read no plan could use. The whole batch shares one
deadline, so enough of them timed out every cold read and put every agent,
including a known-bad one, on the prior: the floor failed open for every plan.

And each planning stage re-read the live registry on its own, so an agent that
landed while the reputation read was in flight was offered, ranked and even
promoted into a kit slot with no reputation entry at all.

The ledger tests here run the REAL `fetch_reps`, `_read_rep` and read cache;
only the Soroban simulation underneath is faked, so routing meets reputation
reads as they actually arrive — slow, partial, and failing — rather than as a
finished dict.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc, reputation_svc
from app.services.reputation_svc import RepInfo
from app.state import state
from app.stellar import cache as rcache
from app.stellar import client as sc

# The floor, delisting, binding and endpoint rules, on the routing policy they
# were written against (see the fixture).
pytestmark = pytest.mark.usefixtures("pre_pipeline_routing")

FREE_FORM_INTENT = "write a haiku about databases"
KIT_INTENT = "tetris game in html"
SEEDED = 12
BAD = "agt_04m1"
USDC = reputation_svc.STROOPS_PER_USDC


async def _no_sleep(*_a: object, **_k: object) -> None:
    return None


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh seeded registry and an empty bound set, both restored after."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(binding_registry, "_bound_ids", set())
    rcache.clear()
    yield
    state.agents.clear()
    state.agents.update(saved)
    rcache.clear()


def _spam(count: int) -> None:
    """Permissionless registrations: never bound, every other one delisted."""
    for i in range(count):
        state.add_agent(
            Agent(
                id=f"ext_spam{i:04d}",
                name="spam",
                skills=["x"],
                price=0.01,
                rep=5.0,
                status="offline" if i % 2 else "online",
                runs=0,
                source="onchain",
            )
        )


def _planner(*agent_ids: str, prompts: list[str] | None = None) -> Any:
    async def _arun(prompt: str) -> SimpleNamespace:
        if prompts is not None:
            prompts.append(prompt)
        steps = [PlanStep(agent_id=a, rationale="r", est_price_usdc=0.0, est_eta_seconds=1.0) for a in agent_ids]
        return SimpleNamespace(content=Plan(steps=steps))

    return _arun


def _prior_reps(ids: list[str]) -> dict[str, RepInfo]:
    return {i: reputation_svc._prior_info(i) for i in ids}


def test_reputation_is_read_only_for_listed_dispatchable_agents(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Unbound spam, delisted spam, and one delisted agent that IS bound: none
    # of them can be routed, so none of them is worth a read.
    _spam(40)
    state.add_agent(state.agents["agt_03d9"].model_copy(update={"status": "offline"}))
    binding_registry.note_bound("ext_spam0001")  # bound, but delisted (odd index)
    asked: list[list[str]] = []

    async def _fake_reps(ids: list[str], *_a: object, **_k: object) -> dict[str, RepInfo]:
        asked.append(list(ids))
        return _prior_reps(ids)

    monkeypatch.setattr(reputation_svc, "fetch_reps", _fake_reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _planner("agt_11c0"))

    asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))

    assert len(asked) == 1
    assert sorted(asked[0]) == sorted(a.id for a in state.list_agents() if a.source == "seeded" and a.id != "agt_03d9")


@pytest.fixture()
def ledger(seeded: None, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The real reputation read path over a fake Soroban simulation.

    `bad` agents carry heavy sub-floor evidence, `failing` agents' reads raise,
    and every read costs `delay` seconds of blocking I/O, as a simulation does.
    """
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger="CFAKELEDGER"))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    cfg = SimpleNamespace(bad=set(), failing=set(), delay=0.0, read=[])

    def _simulate(_contract: str, fn: str, args: list[str], **_kw: object) -> dict[str, int]:
        assert fn == "rep_state"
        agent_id = args[0]
        cfg.read.append(agent_id)
        time.sleep(cfg.delay)
        if agent_id in cfg.failing:
            raise ConnectionError("rpc refused")
        if agent_id in cfg.bad:
            weight = 20 * USDC  # 20 USDC of 10/100 ratings
            return {"sum_w": weight * 1000, "weight": weight, "count": 40, "disputed": 40}
        return {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0}

    monkeypatch.setattr(sc, "simulate_read", _simulate)
    return cfg


def _decompose_on_the_production_pool(intent: str) -> DecomposeResponse:
    """decompose() with the default executor bounded as the lifespan bounds it."""

    async def _go() -> DecomposeResponse:
        pool = ThreadPoolExecutor(max_workers=8)
        asyncio.get_running_loop().set_default_executor(pool)
        try:
            return await orchestrator_svc.decompose(intent)
        finally:
            pool.shutdown(wait=False)

    return asyncio.run(_go())


def test_registry_spam_does_not_push_the_plan_onto_the_prior(
    ledger: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 250 unroutable registrations at 80 ms a read used to be 262 reads
    # through 8 threads against a 2.5 s deadline: the batch timed out, every
    # agent got the prior, and the known-bad agent below was routed.
    ledger.bad = {BAD}
    ledger.delay = 0.08
    _spam(250)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _planner(BAD, "agt_11c0"))

    resp = _decompose_on_the_production_pool(FREE_FORM_INTENT)

    assert len(ledger.read) == SEEDED
    assert not any(agent_id.startswith("ext_spam") for agent_id in ledger.read)
    assert resp.reputation_degraded is False
    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]
    assert ("excluded", "below_floor", BAD) in [(n.kind, n.reason_code, n.agent_id) for n in resp.notices]


def test_a_partially_failing_read_degrades_only_the_agents_it_failed_for(
    ledger: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Routing against a real, partly broken read: two agents' simulations
    # raise, the known-bad agent's succeeds. The failures are served the prior
    # and flagged; the evidence that DID arrive still decides the floor.
    ledger.bad = {BAD}
    ledger.failing = {"agt_03d9", "agt_12r0"}
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _planner(BAD, "agt_03d9"))

    resp = _decompose_on_the_production_pool(FREE_FORM_INTENT)

    assert resp.reputation_degraded is True
    assert [(s.agent_id, s.rep_degraded, s.rep_source) for s in resp.steps] == [("agt_03d9", True, "prior")]
    note = next(n for n in resp.notices if n.agent_id == BAD)
    assert (note.kind, note.reason_code) == ("excluded", "below_floor")
    assert note.lower_bound_bps is not None and note.lower_bound_bps < settings.reputation_floor_bps


def test_an_agent_registered_during_the_reputation_read_is_not_offered(
    seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `registry_sync` lands a new, bound agent while the batch read is in
    # flight. It claims a perfect 5.0 and the floor is one nobody clears, so
    # the only way it reaches the plan is by skipping the floor.
    prompts: list[str] = []

    async def _fake_reps(ids: list[str], *_a: object, **_k: object) -> dict[str, RepInfo]:
        state.add_agent(
            Agent(
                id="ext_new",
                name="new",
                skills=["copy"],
                price=0.01,
                rep=5.0,
                status="online",
                runs=0,
                source="onchain",
            )
        )
        binding_registry.note_bound("ext_new")
        return _prior_reps(ids)

    monkeypatch.setattr(settings, "reputation_floor_bps", 9_900)
    monkeypatch.setattr(reputation_svc, "fetch_reps", _fake_reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _planner("ext_new", prompts=prompts))

    resp = asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))

    assert "ext_new" not in prompts[0]
    assert "ext_new" not in [s.agent_id for s in resp.steps]
    assert resp.planner_fallback is True


def test_an_agent_registered_during_kit_planning_is_never_a_substitute(
    seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The kit path pauses before it plans. An agent landing in that pause
    # shares the sub-floor brief agent's skill and claims a perfect 5.0; the
    # copywriter has 9000 bps of evidence and must take the slot.
    async def _land_during_the_pause(*_a: object, **_k: object) -> None:
        state.add_agent(
            Agent(
                id="ext_seo",
                name="seo-bot",
                skills=["seo"],
                price=0.01,
                rep=5.0,
                status="online",
                runs=0,
                source="onchain",
            )
        )
        binding_registry.note_bound("ext_seo")

    async def _fake_reps(ids: list[str], *_a: object, **_k: object) -> dict[str, RepInfo]:
        reps = {
            i: RepInfo(
                agent_id=i,
                smoothed_bps=9000,
                lower_bound_bps=8000,
                avg_bps=9000,
                count=50,
                weight=50 * USDC,
                disputed=0,
                dispute_rate_bps=0,
                source="onchain",
            )
            for i in ids
        }
        reps["agt_05x7"] = reps["agt_05x7"].model_copy(update={"lower_bound_bps": 100})
        return reps

    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _land_during_the_pause)
    monkeypatch.setattr(reputation_svc, "fetch_reps", _fake_reps)

    resp = asyncio.run(orchestrator_svc.decompose(KIT_INTENT))

    sub = next(s for s in resp.steps if s.substituted_for == "agt_05x7")
    assert sub.agent_id == "agt_01h8"
    assert sub.rep_bps == 9000
    assert "ext_seo" not in [s.agent_id for s in resp.steps]
