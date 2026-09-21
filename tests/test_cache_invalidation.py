"""Per-key invalidation of the Soroban read cache (story 4.04, BLO-32).

A dispute rating that lands on-chain has to reach the very next decompose, and
the read cache stands in its way three times over: the stored entry would serve
the pre-dispute score for the rest of its TTL, a read already in flight when
the rating landed would write that score straight back after the invalidation,
and a caller arriving after the invalidation could join that same flight and
be handed the pre-dispute score directly.

Each race is driven with an `asyncio.Event`, so the stale read is genuinely
parked mid-flight at the moment of invalidation rather than merely slow — a
sleep-based test passes or fails on scheduler timing, and a race test that can
pass by luck proves nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import reputation_svc
from app.services.reputation_svc import STROOPS_PER_USDC
from app.stellar import cache
from app.stellar import client as sc

# rep_state as the ledger returns it: sum_w is rating-bps x weight. Before the
# dispute, four good ratings over 10 USDC of work; after it, the same plus one
# 0/100 dispute rating weighted at a 1 USDC step.
PRE_DISPUTE = {"sum_w": 9000 * 10 * STROOPS_PER_USDC, "weight": 10 * STROOPS_PER_USDC, "count": 4, "disputed": 0}
POST_DISPUTE = {"sum_w": 9000 * 10 * STROOPS_PER_USDC, "weight": 11 * STROOPS_PER_USDC, "count": 5, "disputed": 1}


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def _parked(value: object):
    """A producer that parks mid-flight until released.

    `started` is set once the read is in progress and `release` lets it
    return, so a test can invalidate at an exact point inside the flight.
    Must be built inside the running loop that uses it.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        started.set()
        await release.wait()
        return value

    return producer, started, release, calls


def _returning(value: object):
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        return value

    return producer, calls


def test_invalidate_drops_the_stored_value():
    stale, _ = _returning("pre-dispute")
    fresh, fresh_calls = _returning("post-dispute")

    async def run():
        await cache.get_or_set("k", 60.0, stale)
        cache.invalidate("k")
        assert "k" not in cache._store
        return await cache.get_or_set("k", 60.0, fresh)

    # Well inside the 60 s TTL, so only the invalidation explains the re-read.
    assert asyncio.run(run()) == "post-dispute"
    assert fresh_calls["n"] == 1


def test_a_read_in_flight_at_invalidation_does_not_write_back():
    """The stale write-back: the read started before the rating landed and
    finished after the key was invalidated. Letting it land would restore the
    pre-dispute score and undo the invalidation without anyone noticing."""
    fresh, fresh_calls = _returning("post-dispute")

    async def run():
        stale, started, release, _ = _parked("pre-dispute")
        caller = asyncio.create_task(cache.get_or_set("k", 60.0, stale))
        await started.wait()  # the read is mid-flight...
        cache.invalidate("k")  # ...when the rating lands
        release.set()
        assert await caller == "pre-dispute"
        # The flight has finished; had it written back, it would be here.
        assert "k" not in cache._store
        return await cache.get_or_set("k", 60.0, fresh)

    assert asyncio.run(run()) == "post-dispute"
    assert fresh_calls["n"] == 1


def test_a_caller_after_invalidation_does_not_join_the_stale_flight():
    """Joining the stale flight: the old read is still parked in-flight when a
    new caller arrives. Joining it would hand that caller the pre-dispute score
    — here it would also block until the timeout, since the old read is never
    released until the new caller has its answer."""
    fresh, fresh_calls = _returning("post-dispute")

    async def run():
        stale, started, release, _ = _parked("pre-dispute")
        early = asyncio.create_task(cache.get_or_set("k", 60.0, stale))
        await started.wait()
        cache.invalidate("k")
        late = await asyncio.wait_for(cache.get_or_set("k", 60.0, fresh), timeout=1.0)
        release.set()
        return late, await early

    late, early = asyncio.run(run())
    assert late == "post-dispute"
    assert early == "pre-dispute"
    assert fresh_calls["n"] == 1


def test_callers_already_awaiting_the_stale_flight_still_get_their_answer():
    """Detached, not cancelled. The callers already waiting asked before the
    rating landed, and the answer that was true then is a correct answer for
    them; cancelling the flight would turn it into an error for no gain."""

    async def run():
        stale, started, release, calls = _parked("pre-dispute")
        callers = [asyncio.create_task(cache.get_or_set("k", 60.0, stale)) for _ in range(3)]
        await started.wait()
        flight = cache._flights["k"]
        cache.invalidate("k")
        assert "k" not in cache._flights
        assert not flight.done()
        release.set()
        return await asyncio.gather(*callers), flight, calls["n"]

    results, flight, produced = asyncio.run(run())
    assert results == ["pre-dispute"] * 3
    assert produced == 1  # they all shared the one flight
    assert not flight.cancelled()
    assert flight.result() == "pre-dispute"


def test_the_stale_read_landing_after_the_fresh_one_does_not_overwrite_it():
    """The ordering a bounded generation map could get wrong. The fresh read
    lands first while the stale one is still running; if that dropped the
    key's generation, the counter would restart at the number the stale
    flight captured and its late write would sail through."""
    fresh, _ = _returning("post-dispute")

    async def run():
        stale, started, release, _ = _parked("pre-dispute")
        early = asyncio.create_task(cache.get_or_set("k", 60.0, stale))
        await started.wait()
        cache.invalidate("k")
        # Bounded: were the fresh caller to join the parked stale flight, this
        # would otherwise wait forever instead of failing.
        await asyncio.wait_for(cache.get_or_set("k", 60.0, fresh), timeout=1.0)
        release.set()
        await early
        return cache._store["k"][1]

    assert asyncio.run(run()) == "post-dispute"


def test_invalidating_one_key_leaves_every_other_key_alone():
    value, _ = _returning("v")

    async def failing():
        raise RuntimeError("rpc down")

    async def run():
        busy, started, release, _ = _parked("busy-value")
        in_flight = asyncio.create_task(cache.get_or_set("busy", 60.0, busy))
        await started.wait()
        await cache.get_or_set("other", 60.0, value)
        with pytest.raises(RuntimeError):
            await cache.get_or_set("failed", 60.0, failing)
        await cache.get_or_set("k", 60.0, value)

        cache.invalidate("k")

        assert "other" in cache._store
        assert "failed" in cache._failures
        assert "busy" in cache._flights
        release.set()
        await in_flight
        return cache._store["busy"][1]

    # The other key's flight was neither detached nor fenced: it still lands.
    assert asyncio.run(run()) == "busy-value"


def test_invalidate_clears_the_failure_cache_for_that_key():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rpc down")
        return "recovered"

    async def run():
        with pytest.raises(RuntimeError):
            await cache.get_or_set("k", 60.0, flaky)
        # Inside the negative window this would re-raise without calling
        # upstream at all; an error from before the change says nothing about
        # the state after it.
        cache.invalidate("k")
        assert "k" not in cache._failures
        return await cache.get_or_set("k", 60.0, flaky)

    assert asyncio.run(run()) == "recovered"
    assert calls["n"] == 2


def test_a_stale_read_that_fails_is_not_negatively_cached():
    """The fence covers failures too: negatively cached, a stale error would
    refuse the first reader after the invalidation for the whole window."""
    fresh, _ = _returning("post-dispute")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def stale():
            started.set()
            await release.wait()
            raise RuntimeError("rpc down before the rating landed")

        early = asyncio.create_task(cache.get_or_set("k", 60.0, stale))
        await started.wait()
        cache.invalidate("k")
        release.set()
        with pytest.raises(RuntimeError):
            await early  # its own caller still hears about it
        assert "k" not in cache._failures
        return await cache.get_or_set("k", 60.0, fresh)

    assert asyncio.run(run()) == "post-dispute"


def test_the_generation_map_is_bounded_by_the_flights_in_progress():
    """Keys are caller-influenced (`repstate:{id}`), so a map that remembered
    every key ever invalidated would grow for the life of the process. It
    holds a key only while a flight for that key is running."""

    async def run():
        # Nothing in flight, nothing to fence, nothing recorded.
        for i in range(cache._MAX_ENTRIES * 2):
            cache.invalidate(f"idle{i}")
        assert cache._generations == {}

        callers, releases = [], []
        for i in range(32):
            producer, started, release, _ = _parked(i)
            callers.append(asyncio.create_task(cache.get_or_set(f"busy{i}", 60.0, producer)))
            await started.wait()
            cache.invalidate(f"busy{i}")
            cache.invalidate(f"busy{i}")  # a second bump, not a second entry
            releases.append(release)
        assert len(cache._generations) == 32

        for release in releases:
            release.set()
        await asyncio.gather(*callers)
        return dict(cache._generations), dict(cache._running)

    generations, running = asyncio.run(run())
    assert generations == {}
    assert running == {}


def test_clear_fences_a_flight_that_is_still_running():
    """clear() empties the cache, so a read in flight across it must not
    repopulate it — it is every key invalidated at once."""

    async def run():
        stale, started, release, _ = _parked("pre-clear")
        caller = asyncio.create_task(cache.get_or_set("k", 60.0, stale))
        await started.wait()
        cache.clear()
        release.set()
        assert await caller == "pre-clear"
        return dict(cache._store), dict(cache._generations), dict(cache._running)

    store, generations, running = asyncio.run(run())
    assert store == {}
    # And the bookkeeping drained with the flight, as for any invalidation.
    assert generations == {}
    assert running == {}


# ── reputation ──────────────────────────────────────────────────


@pytest.fixture()
def ledger(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, int]]:
    """A fake ReputationLedger, read through the REAL cache.

    Patched below the cache (at `simulate_read`) rather than at `get_or_set`,
    which is the seam the other reputation tests use: the point here is that
    the cache itself serves, keeps and drops the right entry. `contract_ids`
    is stubbed rather than called, since it is lru_cached and a fake ledger
    id must not outlive this test. Returns the agent → rep_state map a test
    writes to in order to land a rating.
    """
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger="CFAKELEDGER"))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    chain: dict[str, dict[str, int]] = {}

    def simulate_read(contract_id: str, method: str, args: list[str]) -> dict[str, int]:
        assert (contract_id, method) == ("CFAKELEDGER", "rep_state")
        return dict(chain.get(args[0], {}))

    monkeypatch.setattr(sc, "simulate_read", simulate_read)
    return chain


def test_invalidate_rep_makes_the_next_batch_read_see_the_landed_rating(ledger):
    """`fetch_reps` is the batched read decompose takes its snapshot from.
    `invalidate_rep` has to drop exactly the key `_read_rep` fills — the key
    format lives in one helper so the two cannot drift apart."""
    agent = "agt_01h8"
    ledger[agent] = PRE_DISPUTE

    async def run():
        before = (await reputation_svc.fetch_reps([agent]))[agent]
        ledger[agent] = POST_DISPUTE  # the dispute rating lands on-chain
        cached = (await reputation_svc.fetch_reps([agent]))[agent]
        reputation_svc.invalidate_rep(agent)
        after = (await reputation_svc.fetch_reps([agent]))[agent]
        return before, cached, after

    before, cached, after = asyncio.run(run())
    assert before.source == "onchain" and before.disputed == 0
    # Without the invalidation the TTL cache serves the pre-dispute score —
    # the exact failure this story closes.
    assert cached == before
    assert after.disputed == 1
    assert after.dispute_rate_bps == 2000
    assert after.smoothed_bps < before.smoothed_bps
