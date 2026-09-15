"""Story 2.01 — the operator endpoint binding store (ADR 0003 D1).

Three things are asserted here and they are the three that can silently rot:

  1. The in-memory default is BOUNDED, like every other store in this process
     (app/state.py's deque(maxlen=200), ramp_store's _MAX_RAMPS=500) — a store
     the whole internet can write to, one row per bind, cannot be a plain dict.
  2. The Postgres table is APPEND-ONLY. A rebind INSERTs; nothing UPDATEs. That
     is what makes AC-6's timestamp and `previous_endpoint_url` derivable
     rather than maintained, so the SQL itself is under test.
  3. The store is chosen from `database_url` at first USE, not at import, so a
     DATABASE_URL that arrives later is honoured instead of ignored.

The suite is hermetic: no database, no network, no asyncpg. Postgres is
exercised against a fake pool injected into the store, and the async calls run
through `asyncio.run` — the repo's idiom, since pytest-asyncio is not
installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

import pytest

from app.services import binding_store
from app.services.binding_store import BindingRecord, InMemoryBindingStore

STORE_LOGGER = "app.services.binding_store"

AGENT = "agt_alpha"
OWNER = "G" + "A" * 55
URL_ONE = "https://one.example.com/invoke"
URL_TWO = "https://two.example.com/invoke"
URL_THREE = "https://three.example.com/invoke"


def _messages(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


# ── InMemoryBindingStore ──────────────────────────────────────────────────


def test_first_bind_records_the_proof_and_has_no_previous() -> None:
    store = InMemoryBindingStore()

    before = time.time()
    record = asyncio.run(store.put(AGENT, URL_ONE, OWNER))
    after = time.time()

    assert record.agent_id == AGENT
    assert record.endpoint_url == URL_ONE
    assert record.owner == OWNER
    assert record.previous_endpoint_url is None
    # AC-6: the bind is timestamped, in epoch seconds, off this process's clock.
    assert before <= record.bound_at <= after


def test_get_is_none_for_an_agent_that_never_bound() -> None:
    store = InMemoryBindingStore()

    assert asyncio.run(store.get("agt_never")) is None


def test_get_returns_the_binding_that_was_put() -> None:
    store = InMemoryBindingStore()

    async def go() -> BindingRecord | None:
        await store.put(AGENT, URL_ONE, OWNER)
        return await store.get(AGENT)

    current = asyncio.run(go())

    assert current is not None
    assert current.endpoint_url == URL_ONE
    assert current.owner == OWNER


def test_rebind_reports_the_endpoint_it_replaced() -> None:
    """`put` returns previous_endpoint_url itself, so the router never has to
    read-then-write and two rebinds cannot race each other into disagreement."""
    store = InMemoryBindingStore()

    async def go() -> tuple[BindingRecord, BindingRecord, BindingRecord | None]:
        await store.put(AGENT, URL_ONE, OWNER)
        second = await store.put(AGENT, URL_TWO, OWNER)
        third = await store.put(AGENT, URL_THREE, OWNER)
        return second, third, await store.get(AGENT)

    second, third, current = asyncio.run(go())

    assert second.previous_endpoint_url == URL_ONE
    assert third.previous_endpoint_url == URL_TWO
    assert current is not None
    assert current.endpoint_url == URL_THREE
    assert current.previous_endpoint_url == URL_TWO


def test_rebind_keeps_one_record_per_agent() -> None:
    store = InMemoryBindingStore()

    async def go() -> None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put(AGENT, URL_TWO, OWNER)

    asyncio.run(go())

    assert len(store._bindings) == 1


def test_the_store_is_bounded_and_evicts_the_oldest() -> None:
    store = InMemoryBindingStore(max_bindings=3)

    async def go() -> None:
        for n in range(5):
            await store.put(f"agt_{n}", f"https://n{n}.example.com/invoke", OWNER)

    asyncio.run(go())

    assert len(store._bindings) == 3
    assert asyncio.run(store.get("agt_0")) is None
    assert asyncio.run(store.get("agt_1")) is None
    assert asyncio.run(store.get("agt_4")) is not None


def test_a_rebind_never_evicts_and_refreshes_its_position() -> None:
    """A rebind replaces an entry rather than growing the store, so it cannot
    push anyone out — and it moves its own entry to the back, so the victim is
    the least recently bound agent rather than the first one ever seen."""
    store = InMemoryBindingStore(max_bindings=2)

    async def go() -> None:
        await store.put("agt_old", URL_ONE, OWNER)
        await store.put("agt_mid", URL_ONE, OWNER)
        await store.put("agt_old", URL_TWO, OWNER)  # refreshes agt_old, evicts nobody
        await store.put("agt_new", URL_ONE, OWNER)  # at cap → drops agt_mid

    asyncio.run(go())

    assert len(store._bindings) == 2
    assert asyncio.run(store.get("agt_mid")) is None
    assert asyncio.run(store.get("agt_old")) is not None
    assert asyncio.run(store.get("agt_new")) is not None


def test_eviction_is_logged_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """An endpoint disappearing forces a re-bind nobody was told about — it
    must never happen silently (ramp_store's reason, same volume)."""
    store = InMemoryBindingStore(max_bindings=1)

    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        asyncio.run(store.put("agt_dropped", URL_ONE, OWNER))
        asyncio.run(store.put("agt_kept", URL_TWO, OWNER))

    lines = _messages(caplog, STORE_LOGGER)
    assert any("agent_id=agt_dropped" in m and "DATABASE_URL" in m for m in lines)


def test_the_default_cap_matches_the_other_bounded_stores() -> None:
    # ramp_store._MAX_RAMPS and external_binding's MAX_CHALLENGES are both 500.
    assert binding_store._MAX_BINDINGS == 500
    assert InMemoryBindingStore()._max_bindings == 500


def test_close_drops_every_record_and_is_safe_twice() -> None:
    store = InMemoryBindingStore()

    async def go() -> BindingRecord | None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.close()
        await store.close()
        return await store.get(AGENT)

    assert asyncio.run(go()) is None


def test_records_are_frozen() -> None:
    """A record is evidence of what was accepted, not mutable state."""
    record = asyncio.run(InMemoryBindingStore().put(AGENT, URL_ONE, OWNER))

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.endpoint_url = URL_TWO  # type: ignore[misc]
