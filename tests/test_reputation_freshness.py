"""S8: a reputation read right after a rating lands, through the REAL cache.

`invalidate_rep` used to DROP the agent's stored read. A next read that then
missed the batch deadline had no last known value to fall back on and served
the prior — 6983 became 7000, `source=prior`, `degraded=true`, `disputed=0` —
so the agent a dispute had just landed on cleared the routing floor and its
score went UP on the agents page. Here the stored read is kept as a superseded
fallback the floor refuses, and a background refresh fetches the post-rating
value at once. Only `simulate_read` is faked; a slow read is a thread parked on
an Event the test releases, so nothing depends on wall-clock luck.
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
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services import reputation_svc as rep
from app.state import state
from app.stellar import cache as rcache
from app.stellar import client as sc

USDC = rep.STROOPS_PER_USDC
LEDGER = "CFAKELEDGER"
KIT_INTENT = "tetris game in html"
# Before the dispute: four good ratings over 10 USDC of work, well above the
# floor. After it: heavy negative evidence, well below it — and below the
# prior, which is the whole hazard: the prior clears the floor.
PRE = {"sum_w": 9000 * 10 * USDC, "weight": 10 * USDC, "count": 4, "disputed": 0}
POST = {"sum_w": 9000 * 10 * USDC, "weight": 30 * USDC, "count": 5, "disputed": 1}


class Chain:
    """A scriptable ReputationLedger. `hold(agent)` parks that agent's reads
    until `release(agent)`; everything else answers at once."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.reads: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def hold(self, agent: str) -> None:
        self.gates[agent] = threading.Event()

    def release(self, agent: str) -> None:
        self.gates[agent].set()

    def count(self, agent: str) -> int:
        with self._lock:
            return self.reads.count(agent)

    def simulate_read(self, contract_id: str, method: str, args: list[Any], **_kw: Any) -> Any:
        assert (contract_id, method) == (LEDGER, "rep_state")
        agent = args[0]
        with self._lock:
            self.reads.append(agent)
        gate = self.gates.get(agent)
        if gate is not None:
            assert gate.wait(10), f"{agent}'s read was never released"
        value = self.state.get(agent, {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0})
        if isinstance(value, BaseException):
            raise value
        return dict(value)


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
    rep._refreshes.clear()
    rep._refresh_again.clear()


def _key(agent: str) -> str:
    return rep._rep_cache_key(agent)


async def _join_refresh(agent: str) -> None:
    task = rep._refreshes.get(agent)
    if task is not None:
        await asyncio.wait({task})


# ── invalidate keeps the fallback ───────────────────────────────


def test_invalidate_keeps_the_last_read_as_a_superseded_fallback(chain):
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.hold("agt_01h8")  # the refresh the invalidation starts parks
        rep.invalidate_rep("agt_01h8")
        kept = rcache.last_stored(_key("agt_01h8"))
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")
        return kept

    kept = asyncio.run(scenario())

    assert kept is not None
    assert kept.superseded is True
    assert kept.value == PRE


def test_a_slow_read_after_invalidation_serves_the_last_value_stale_never_the_prior(chain):
    """The audit's S8 reproduction: the read after the rating misses the batch
    deadline. The agent keeps its real, older score — with its age — and is
    never scored on the prior."""
    chain.state["agt_01h8"] = PRE

    async def scenario():
        before = (await rep.fetch_reps(["agt_01h8"]))["agt_01h8"]
        chain.state["agt_01h8"] = POST  # the dispute rating lands on-chain
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        served = (await rep.fetch_reps(["agt_01h8"], timeout_seconds=0.2))["agt_01h8"]
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")
        return before, served

    before, served = asyncio.run(scenario())

    assert served.source == "onchain"
    assert served.degraded is False
    assert served.stale is True and served.superseded is True
    assert served.stale_age_seconds is not None and 0.0 <= served.stale_age_seconds < 5.0
    assert (served.smoothed_bps, served.count, served.disputed) == (before.smoothed_bps, 4, 0)
    assert served.smoothed_bps != settings.reputation_prior_bps
    assert rep.passes_floor(served) is False


def test_a_superseded_read_is_served_even_past_the_stale_grace(chain, monkeypatch):
    """The grace bounds how old evidence the floor may JUDGE. A superseded read
    is refused by the floor whatever its age, so the grace has nothing to
    protect — and applying it would hand the just-disputed agent the prior."""
    monkeypatch.setattr(settings, "reputation_stale_grace_seconds", 0.0)
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        key = _key("agt_01h8")
        _expiry, value = rcache._store[key]
        rcache._store[key] = (time.monotonic() - 600.0, value)  # ten minutes past its TTL
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        served = (await rep.fetch_reps(["agt_01h8"], timeout_seconds=0.2))["agt_01h8"]
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")
        return served

    served = asyncio.run(scenario())

    assert served.degraded is False
    assert served.stale is True and served.superseded is True
    assert served.stale_age_seconds is not None and served.stale_age_seconds >= 600.0
    assert rep.passes_floor(served) is False


def test_a_fast_read_after_invalidation_serves_the_post_rating_value_fresh(chain):
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.state["agt_01h8"] = POST
        rep.invalidate_rep("agt_01h8")
        return (await rep.fetch_reps(["agt_01h8"]))["agt_01h8"]

    served = asyncio.run(scenario())

    assert (served.count, served.disputed) == (5, 1)
    assert served.stale is False and served.superseded is False and served.degraded is False
    assert rep.passes_floor(served) is False  # judged, and failed, on its own post-dispute numbers


def test_a_cold_agent_with_no_known_value_is_still_the_degraded_prior(chain):
    """Nothing was ever read, so there is nothing to fall back on: a genuinely
    unknown agent is still the prior, marked degraded — invalidated or not."""

    async def scenario():
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        served = (await rep.fetch_reps(["agt_01h8"], timeout_seconds=0.2))["agt_01h8"]
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")
        return served

    served = asyncio.run(scenario())

    assert served.source == "prior"
    assert served.degraded is True
    assert served.stale is False and served.superseded is False


def test_the_warning_says_a_superseded_agent_is_refused_not_judged(chain, caplog):
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        with caplog.at_level(logging.WARNING, logger="app.services.reputation_svc"):
            await rep.fetch_reps(["agt_01h8"], timeout_seconds=0.2)
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")

    asyncio.run(scenario())

    lines = [r.getMessage() for r in caplog.records if r.name == "app.services.reputation_svc"]
    assert len(lines) == 1
    assert "last known on-chain value for 1/1 agents [agt_01h8]" in lines[0]
    assert "refuses it until a fresh read answers" in lines[0]
    assert "still applied to that evidence" not in lines[0]
    assert "failing OPEN" not in lines[0]


# ── the background refresh ──────────────────────────────────────


def test_invalidation_refreshes_the_agent_in_the_background(chain):
    """Nobody reads, yet the post-rating value is in the cache moments later —
    the next page load and the next plan do not wait for a TTL or a deadline."""
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.state["agt_01h8"] = POST
        rep.invalidate_rep("agt_01h8")
        await _join_refresh("agt_01h8")
        reads_after_refresh = chain.count("agt_01h8")
        served = (await rep.fetch_reps(["agt_01h8"]))["agt_01h8"]
        return reads_after_refresh, served

    reads_after_refresh, served = asyncio.run(scenario())

    assert reads_after_refresh == 2  # the first read, then the refresh
    assert chain.count("agt_01h8") == 2  # the batch after it was a cache hit
    assert (served.count, served.disputed) == (5, 1)
    stored = rcache.last_stored(_key("agt_01h8"))
    assert stored is not None and stored.superseded is False


def test_invalidation_never_waits_on_the_refresh(chain):
    """`invalidate_rep` runs on rating paths. It returns at once even while
    the ledger read it started is parked."""
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.hold("agt_01h8")
        started = time.monotonic()
        rep.invalidate_rep("agt_01h8")
        elapsed = time.monotonic() - started
        task = rep._refreshes["agt_01h8"]
        pending = not task.done()
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")
        return elapsed, pending

    elapsed, pending = asyncio.run(scenario())

    assert pending is True
    assert elapsed < 0.1


def test_a_plan_arriving_during_the_refresh_joins_its_read(chain):
    """Single flight: the refresh IS the cache's flight for the key, so a batch
    that arrives meanwhile shares it instead of reading the ledger again."""
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.state["agt_01h8"] = POST
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        batch = asyncio.ensure_future(rep.fetch_reps(["agt_01h8"], timeout_seconds=5.0))
        await asyncio.sleep(0.05)
        chain.release("agt_01h8")
        served = (await batch)["agt_01h8"]
        await _join_refresh("agt_01h8")
        return served

    served = asyncio.run(scenario())

    assert chain.count("agt_01h8") == 2  # the first read, then ONE shared read
    assert (served.count, served.disputed) == (5, 1)


def test_repeated_invalidation_keeps_one_refresh_that_goes_round_once_more(chain):
    """A second rating landing while the refresh is parked detaches that read
    — it predates the second rating — so the one refresh task reads again,
    once, and the cache ends on the latest value."""
    chain.state["agt_01h8"] = PRE

    async def scenario():
        await rep.fetch_reps(["agt_01h8"])
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        await asyncio.sleep(0.05)  # the refresh's read is parked in the pool
        first = rep._refreshes["agt_01h8"]
        chain.state["agt_01h8"] = POST
        rep.invalidate_rep("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        same = rep._refreshes["agt_01h8"] is first
        chain.release("agt_01h8")
        await _join_refresh("agt_01h8")
        return same

    same = asyncio.run(scenario())

    assert same is True
    assert chain.count("agt_01h8") == 3  # the first read, the parked one, one more
    stored = rcache.last_stored(_key("agt_01h8"))
    assert stored is not None and stored.superseded is False
    assert stored.value == POST


def test_the_refreshes_are_bounded(chain, monkeypatch):
    monkeypatch.setattr(rep, "_MAX_REFRESHES", 2)
    ids = ["agt_a", "agt_b", "agt_c"]

    async def scenario():
        for agent in ids:
            chain.hold(agent)
            rep.invalidate_rep(agent)
        running = sorted(rep._refreshes)
        for agent in ids:
            chain.release(agent)
        for agent in ids:
            await _join_refresh(agent)
        return running

    running = asyncio.run(scenario())

    assert running == ["agt_a", "agt_b"]
    assert rep._refreshes == {}


def test_invalidation_without_a_running_loop_starts_nothing(chain):
    """A synchronous caller has no loop to run a refresh on. The superseded
    entry already sends the next reader to the ledger."""
    rep.invalidate_rep("agt_01h8")
    assert rep._refreshes == {}


def test_shutdown_cancels_a_refresh_still_running(chain):
    chain.state["agt_01h8"] = PRE

    async def scenario():
        chain.hold("agt_01h8")
        rep.invalidate_rep("agt_01h8")
        task = rep._refreshes["agt_01h8"]
        await rep.stop_refreshes()
        chain.release("agt_01h8")
        return task

    task = asyncio.run(scenario())

    assert task.cancelled()
    assert rep._refreshes == {}


# ── the planner never routes a just-disputed agent on the prior ─


async def _noop(*_a: object, **_k: object) -> None:
    return None


@pytest.fixture()
def seeded(monkeypatch):
    """Fresh seeded registry, restored after; kit thinking-sleep no-op'd."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(orchestrator_svc, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": _noop}))
    yield
    state.agents.clear()
    state.agents.update(saved)


def _routed_cleanly(resp: orchestrator_svc.DecomposeResponse, agent_id: str) -> bool:
    """Whether the plan offers `agent_id` as a step that CLEARED the floor."""
    return any(s.agent_id == agent_id and not s.degraded for s in resp.steps)


def test_the_planner_never_routes_a_just_disputed_agent_on_the_prior(chain, seeded, monkeypatch):
    """Dispute rating → invalidate → slow chain → plan. The prior clears the
    floor (5677 vs 5500 bps), so the old drop-then-prior path offered the
    disputed agent as cleanly routable. Now the plan either refuses it (while
    only its superseded pre-dispute read is known) or judges it on its
    post-dispute read — and it clears the floor on neither."""
    monkeypatch.setattr(settings, "reputation_batch_timeout_seconds", 0.2)
    for agent in state.list_agents():
        chain.state[agent.id] = PRE

    async def scenario():
        first = await orchestrator_svc.decompose(KIT_INTENT)
        disputed = first.steps[0].agent_id
        chain.state[disputed] = POST  # the dispute rating lands on-chain
        chain.hold(disputed)  # ...and the chain is slower than the deadline
        rep.invalidate_rep(disputed)
        during = await orchestrator_svc.decompose(KIT_INTENT)
        chain.release(disputed)
        await _join_refresh(disputed)
        after = await orchestrator_svc.decompose(KIT_INTENT)
        return disputed, first, during, after

    disputed, first, during, after = asyncio.run(scenario())

    assert _routed_cleanly(first, disputed)
    # While the refresh is parked: refused, never the prior.
    assert not _routed_cleanly(during, disputed)
    assert all(s.rep_source != "prior" for s in during.steps if s.agent_id == disputed)
    assert any(n.agent_id == disputed for n in during.notices)
    # Once the refresh has landed: judged on the post-dispute read.
    assert not _routed_cleanly(after, disputed)
    notice = next(n for n in after.notices if n.agent_id == disputed)
    assert (notice.count, notice.lower_bound_bps is not None) == (5, True)
    assert notice.lower_bound_bps < settings.reputation_floor_bps
