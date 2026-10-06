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
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services import reputation_svc as rep
from app.stellar import cache as rcache
from app.stellar import client as sc

# The floor, delisting, binding and endpoint rules, on the routing policy they
# were written against (see the fixture).
pytestmark = pytest.mark.usefixtures("pre_pipeline_routing")

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


# ── past the deadline, the last known read beats the prior ──────


def _expire(agent: str, seconds_ago: float) -> None:
    """Age an agent's stored read as if its TTL ran out `seconds_ago`."""
    key = rep._rep_cache_key(agent)
    _expiry, value = rcache._store[key]
    rcache._store[key] = (time.monotonic() - seconds_ago, value)


def test_a_just_expired_read_is_served_stale_when_the_refresh_is_slow(chain):
    """A warm host's first read after the TTL used to miss the deadline and
    score the agent on the prior — which clears the floor. Its last on-chain
    read is served instead, marked stale with its age, so a sub-floor agent
    stays sub-floor; once the slow refresh lands, the next read is fresh."""
    chain.state["agt_04m1"] = BAD

    async def scenario():
        fresh = await rep.fetch_reps(["agt_04m1"])
        _expire("agt_04m1", 2.0)
        chain.hold("agt_04m1")
        served = await rep.fetch_reps(["agt_04m1"], timeout_seconds=0.3)
        chain.release("agt_04m1")
        await asyncio.wait({_flight("agt_04m1")})
        after = await rep.fetch_reps(["agt_04m1"])
        return fresh["agt_04m1"], served["agt_04m1"], after["agt_04m1"]

    fresh, served, after = asyncio.run(scenario())

    assert served.stale is True
    assert served.degraded is False
    assert served.source == "onchain"
    assert served.model_dump(exclude={"stale", "stale_age_seconds"}) == fresh.model_dump(
        exclude={"stale", "stale_age_seconds"}
    )
    assert rep.passes_floor(served) is False
    # Read TTL (15 s), plus the 2 s it had been expired, plus the 0.3 s the
    # batch waited before serving it — and not a great deal more.
    ttl = settings.reputation_read_ttl_seconds
    assert ttl + 2.3 <= served.stale_age_seconds <= ttl + 3.3
    assert fresh.stale is False and fresh.stale_age_seconds is None
    assert after.stale is False and after.degraded is False


def test_a_read_expired_past_the_grace_degrades_to_the_prior(chain, monkeypatch):
    monkeypatch.setattr(settings, "reputation_stale_grace_seconds", 60.0)
    chain.state["agt_04m1"] = BAD

    async def scenario():
        await rep.fetch_reps(["agt_04m1"])
        _expire("agt_04m1", 61.0)
        chain.hold("agt_04m1")
        served = await rep.fetch_reps(["agt_04m1"], timeout_seconds=0.3)
        chain.release("agt_04m1")
        await asyncio.wait({_flight("agt_04m1")})
        return served["agt_04m1"]

    served = asyncio.run(scenario())

    assert served.stale is False
    assert served.degraded is True
    assert served.source == "prior"


def test_a_failed_refresh_serves_the_last_read_stale(chain, caplog):
    """A read that errors rather than hangs gets the same treatment, and the
    batch says so on its own line — not the fail-OPEN one, since nothing is."""
    chain.state["agt_04m1"] = BAD

    async def scenario():
        await rep.fetch_reps(["agt_04m1"])
        _expire("agt_04m1", 1.0)
        chain.state["agt_04m1"] = RuntimeError("rpc down")
        return await rep.fetch_reps(["agt_04m1", "agt_new"])

    with caplog.at_level(logging.DEBUG, logger="app.services.reputation_svc"):
        infos = asyncio.run(scenario())

    assert infos["agt_04m1"].stale is True
    assert infos["agt_04m1"].degraded is False
    lines = [r.getMessage() for r in caplog.records if r.name == "app.services.reputation_svc"]
    assert len(lines) == 1
    assert "last known on-chain value for 1/2 agents [agt_04m1]" in lines[0]
    assert "rpc down" in lines[0]
    assert "failing OPEN" not in lines[0]


def test_an_invalidated_read_is_served_superseded_never_the_prior(chain):
    """S8. Invalidation says a rating landed after the stored read. When the
    fresh read then misses the deadline, the agent used to be served the prior
    — its score went UP and it cleared the floor. Now it is served its last
    on-chain read, stale with its age and superseded, which the floor refuses."""
    chain.state["agt_04m1"] = GOOD

    async def scenario():
        before = (await rep.fetch_reps(["agt_04m1"]))["agt_04m1"]
        rep.invalidate_rep("agt_04m1")
        chain.hold("agt_04m1")
        served = await rep.fetch_reps(["agt_04m1"], timeout_seconds=0.3)
        chain.release("agt_04m1")
        await asyncio.wait({_flight("agt_04m1")})
        return before, served["agt_04m1"]

    before, served = asyncio.run(scenario())

    assert served.degraded is False
    assert served.source == "onchain"
    assert served.stale is True
    assert served.stale_age_seconds is not None and 0.0 <= served.stale_age_seconds < 5.0
    assert served.superseded is True
    assert served.model_dump(exclude={"stale", "stale_age_seconds"}) == before.model_dump(
        exclude={"stale", "stale_age_seconds"}
    )
    # GOOD clears the floor on its numbers; superseded, it is refused anyway.
    assert rep.passes_floor(before) is True
    assert rep.passes_floor(served) is False


def _join_prewarm(client) -> None:
    """Wait, on the app's own loop, for the boot pre-warm to finish."""
    task = rep._prewarm_task
    if task is None:
        return

    async def join() -> None:
        await asyncio.wait({task})

    client.portal.call(join)


def test_stale_rows_reach_the_client(chain, client):
    """On the wire, where the router's mirror model would drop an undeclared
    field without a word."""
    _join_prewarm(client)
    chain.state["agt_01h8"] = GOOD
    rep.invalidate_rep("agt_01h8")  # the boot pre-warm read it before GOOD was set
    assert client.get("/api/stellar/reputation/agt_01h8").json()["stale"] is False
    _expire("agt_01h8", 1.0)
    chain.state["agt_01h8"] = RuntimeError("rpc down")

    body = client.get("/api/stellar/reputation/agt_01h8").json()

    assert body["stale"] is True
    assert body["degraded"] is False
    assert body["source"] == "onchain"
    ttl = settings.reputation_read_ttl_seconds
    assert ttl + 1.0 <= body["stale_age_seconds"] <= ttl + 2.0


# ── the boot pre-warm ───────────────────────────────────────────


def test_boot_prewarms_every_registered_agent(chain):
    """The first plan after a deploy used to be the first reader of every
    agent. Now lifespan reads them all once, in the background, and the first
    batch is served from cache without another read."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.state import state

    with TestClient(app) as client:
        _join_prewarm(client)
        ids = {a.id for a in state.list_agents()}
        assert sorted(chain.reads) == sorted(ids)
        body = client.get("/api/stellar/reputation").json()

    assert sorted(chain.reads) == sorted(ids), "the first batch after boot read nothing again"
    assert not any(info["degraded"] for info in body["reputations"].values())


def test_boot_does_not_wait_for_the_prewarm(chain):
    """The request that woke the instance must not queue behind the chain."""
    from fastapi.testclient import TestClient

    from app.main import app

    chain.hold("agt_01h8")
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert rep._prewarm_task is not None and not rep._prewarm_task.done()
            chain.release("agt_01h8")
            _join_prewarm(client)
    finally:
        chain.release("agt_01h8")


def test_no_prewarm_without_a_ledger(client):
    """The hermetic default: nothing configured, nothing read, no task."""
    assert rep._prewarm_task is None


# ── an unregistered id costs no RPC ─────────────────────────────


def test_an_unregistered_id_is_404_without_a_chain_read(chain, client):
    """The audit's flood: 40 unregistered ids were 40 x 200 and 40 upstream
    reads, and 120 queued ahead of the registry batch degraded all 23 agents.
    Now each is refused before the chain is asked anything."""
    _join_prewarm(client)
    before = len(chain.reads)

    codes = {client.get(f"/api/stellar/reputation/nobody_{i}").status_code for i in range(40)}

    assert codes == {404}
    assert client.get("/api/stellar/reputation/nobody_0").json()["detail"] == "unknown_agent"
    assert len(chain.reads) == before, "an unregistered id reached the RPC"
    # A registered agent is still read and served.
    assert client.get("/api/stellar/reputation/agt_01h8").status_code == 200


# ── the read is cached for the configured TTL ───────────────────


@pytest.mark.parametrize("ttl", [15.0, 42.0])
def test_a_read_is_cached_for_the_configured_ttl(chain, monkeypatch, ttl):
    """Audit mutant M4: the TTL was hard-coded to an hour and nothing noticed.
    The stored entry expires REPUTATION_READ_TTL_SECONDS after the read."""
    monkeypatch.setattr(settings, "reputation_read_ttl_seconds", ttl)
    before = time.monotonic()
    asyncio.run(rep.fetch_reps(["agt_01h8"]))
    after = time.monotonic()

    expiry, _value = rcache._store[rep._rep_cache_key("agt_01h8")]
    assert before + ttl <= expiry <= after + ttl
