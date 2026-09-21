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

import pytest

from app.stellar import cache


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
