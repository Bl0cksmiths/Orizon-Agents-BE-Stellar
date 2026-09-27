"""The reputation read, driven through the REAL cache with only the RPC faked.

Every older degradation test replaced `cache.get_or_set` wholesale, so the
properties that decide what the routing floor actually sees — which reads a
deadline keeps, whether an aborted read still lands, what a failed read leaves
in the cache — were never exercised together. Here `simulate_read` is the only
stand-in, and a slow read is a thread parked on an Event the test releases, so
nothing waits on wall-clock luck.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services import reputation_svc as rep
from app.stellar import cache as rcache
from app.stellar import client as sc

USDC = rep.STROOPS_PER_USDC
LEDGER = "CFAKELEDGER"
# Heavy negative evidence: 20 USDC of 10/100 ratings, well under the floor.
BAD = {"sum_w": 1000 * 20 * USDC, "weight": 20 * USDC, "count": 40, "disputed": 0}
GOOD = {"sum_w": 9000 * 10 * USDC, "weight": 10 * USDC, "count": 4, "disputed": 0}


class Chain:
    """A scriptable ReputationLedger. `hold(agent)` parks that agent's reads
    until `release(agent)`; everything else answers at once."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.reads: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def hold(self, agent: str) -> None:
        self.gates[agent] = threading.Event()

    def release(self, agent: str) -> None:
        self.gates[agent].set()

    def simulate_read(self, contract_id: str, method: str, args: list[Any], **kwargs: Any) -> Any:
        assert (contract_id, method) == (LEDGER, "rep_state")
        agent = args[0]
        with self._lock:
            self.reads.append(agent)
            self.kwargs.append(kwargs)
        gate = self.gates.get(agent)
        if gate is not None:
            assert gate.wait(10), f"{agent}'s read was never released"
        value = self.state.get(agent, {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0})
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture()
def chain(monkeypatch) -> Chain:
    fake = Chain()
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger=LEDGER))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    monkeypatch.setattr(sc, "simulate_read", fake.simulate_read)
    rcache.clear()
    yield fake
    for gate in fake.gates.values():
        gate.set()
    rcache.clear()


def test_a_reputation_read_is_one_round_trip(chain):
    """rep_state is a view: the read asks the client to skip load_account."""
    asyncio.run(rep.fetch_reps(["agt_01h8"]))

    assert chain.kwargs == [{"load_source": False}]


# ── a malformed answer is a failure, not a cached success ───────


def test_a_non_map_answer_is_negatively_cached_not_stored_for_the_ttl(chain):
    """`simulate_read` returns None for an empty result set. Stored as a
    success, that None was read back as degraded on every hit for the whole
    15 s TTL; raised inside the producer it is a failure, held only for the
    cache's short negative window and then retried."""
    chain.state["agt_01h8"] = None
    key = rep._rep_cache_key("agt_01h8")

    infos = asyncio.run(rep.fetch_reps(["agt_01h8"]))

    assert infos["agt_01h8"].degraded is True
    assert key not in rcache._store
    assert key in rcache._failures
    expiry, exc_type, message = rcache._failures[key]
    assert exc_type is TypeError
    assert "expected a map" in message
    assert expiry - time.monotonic() <= rcache._NEGATIVE_TTL_SECONDS


# ── a deadline keeps every answer it has ────────────────────────


def _flight(agent: str):
    return rcache._flights[rep._rep_cache_key(agent)]


def test_one_slow_read_degrades_only_that_agent_and_still_lands(chain):
    """The audit's D′ on the real path. One agent's read is held past the
    deadline; every other agent keeps its on-chain answer, the held agent alone
    falls back, and its read — abandoned by the batch, not cancelled — lands in
    the cache so the next batch reads nobody twice."""
    ids = [f"agt_{i:02d}" for i in range(6)]
    for agent in ids:
        chain.state[agent] = GOOD
    chain.state["agt_03"] = BAD
    chain.hold("agt_05")

    async def scenario():
        first = await rep.fetch_reps(ids, timeout_seconds=0.5)
        chain.release("agt_05")
        await asyncio.wait({_flight("agt_05")})
        second = await rep.fetch_reps(ids, timeout_seconds=0.5)
        return first, second

    first, second = asyncio.run(scenario())

    assert {a for a, i in first.items() if i.degraded} == {"agt_05"}
    assert all(first[a].source == "onchain" for a in ids if a != "agt_05")
    assert rep.passes_floor(first["agt_03"]) is False, "a known sub-floor agent must stay sub-floor"
    assert not any(i.degraded for i in second.values())
    assert second["agt_05"].source == "onchain"
    assert sorted(chain.reads) == sorted(ids), "the aborted read landed; nothing was read twice"


def test_a_cached_verdict_survives_a_slow_unrelated_read(chain):
    """The routing audit's P1. A sub-floor agent's state is already cached;
    an unrelated read then hangs. The cached verdict is an answer the batch
    already has, and it must not be traded for the prior."""
    chain.state["agt_04m1"] = BAD
    chain.hold("agt_06q4")

    async def scenario():
        warm = await rep.fetch_reps(["agt_04m1"])
        batch = await rep.fetch_reps(["agt_04m1", "agt_06q4"], timeout_seconds=0.3)
        chain.release("agt_06q4")
        await asyncio.wait({_flight("agt_06q4")})
        return warm, batch

    warm, batch = asyncio.run(scenario())

    assert rep.passes_floor(warm["agt_04m1"]) is False
    assert batch["agt_04m1"].model_dump() == warm["agt_04m1"].model_dump()
    assert batch["agt_06q4"].degraded is True


def test_a_decompose_behind_a_slow_read_still_excludes_the_sub_floor_agent(chain, monkeypatch):
    """The same, one layer up: through `decompose` a known sub-floor agent is
    kept out of the plan while an unrelated read is slow, instead of being
    routed on the prior's 5677."""
    from app.schemas import Plan, PlanStep
    from app.seed import seed_registry
    from app.services import orchestrator_svc
    from app.state import state

    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    chain.state["agt_04m1"] = BAD
    chain.hold("agt_06q4")
    monkeypatch.setattr(settings, "reputation_batch_timeout_seconds", 0.3)

    async def planner(_prompt):
        step = PlanStep(agent_id="agt_04m1", rationale="r", est_price_usdc=0.0, est_eta_seconds=1)
        return SimpleNamespace(content=Plan(steps=[step]))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", planner)

    async def scenario():
        await rep.fetch_reps(["agt_04m1"])
        resp = await orchestrator_svc.decompose("write a haiku about databases")
        chain.release("agt_06q4")
        await asyncio.wait({_flight("agt_06q4")})
        return resp

    try:
        resp = asyncio.run(scenario())
    finally:
        state.agents.clear()
        state.agents.update(saved)

    assert "agt_04m1" not in [s.agent_id for s in resp.steps]
    assert resp.reputation_degraded is True, "the slow agent's prior is still reported"
