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
