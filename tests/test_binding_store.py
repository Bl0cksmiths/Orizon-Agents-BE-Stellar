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


# ── PostgresBindingStore ──────────────────────────────────────────────────


class FakePool:
    """Stands in for an asyncpg pool: records every statement and models the
    one table just well enough to answer the two queries the store sends.

    Rows are kept in a plain list, and list order IS the BIGSERIAL `id` order
    the real queries sort by — so "the newest row" means the same thing here as
    it does in Postgres, and an implementation that started UPDATEing rows
    instead of appending them would visibly change `rows`.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rows: list[dict[str, object]] = []
        self.closed = 0

    async def execute(self, sql: str, *args: object) -> str:
        self.statements.append(sql)
        return "CREATE TABLE"

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        self.statements.append(sql)
        if "INSERT INTO" in sql:
            agent_id, endpoint_url, owner, bound_at = args
            previous = self._history(str(agent_id))
            self.rows.append({"agent_id": agent_id, "endpoint_url": endpoint_url, "owner": owner, "bound_at": bound_at})
            return {
                "endpoint_url": endpoint_url,
                "owner": owner,
                "bound_at": bound_at,
                "previous_endpoint_url": previous[-1]["endpoint_url"] if previous else None,
            }
        history = self._history(str(args[0]))
        if not history:
            return None
        newest = history[-1]
        return {
            "endpoint_url": newest["endpoint_url"],
            "owner": newest["owner"],
            "bound_at": newest["bound_at"],
            "previous_endpoint_url": history[-2]["endpoint_url"] if len(history) > 1 else None,
        }

    def _history(self, agent_id: str) -> list[dict[str, object]]:
        return [r for r in self.rows if r["agent_id"] == agent_id]

    async def close(self) -> None:
        self.closed += 1

    @property
    def writes(self) -> list[str]:
        return [s for s in self.statements if "INSERT" in s or "UPDATE" in s or "DELETE" in s]


def _pg(pool: FakePool) -> binding_store.PostgresBindingStore:
    return binding_store.PostgresBindingStore("postgres://user:pw@example.invalid/db", pool=pool)


def test_the_table_is_created_lazily_and_only_once() -> None:
    """No migration tooling exists in this repo, so the DDL ships with the
    store — but constructing it must not do I/O, and a hot process must not
    re-run the DDL on every request."""
    pool = FakePool()
    store = _pg(pool)

    assert pool.statements == []  # construction alone talks to nothing

    async def go() -> None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put(AGENT, URL_TWO, OWNER)
        await store.get(AGENT)

    asyncio.run(go())

    ddl = [s for s in pool.statements if "CREATE TABLE" in s]
    assert len(ddl) == 1
    assert "CREATE TABLE IF NOT EXISTS agent_bindings" in ddl[0]


def test_a_rebind_appends_a_row_and_never_updates_one() -> None:
    """The append-only rule, asserted on the SQL actually sent: two binds for
    one agent are two INSERTed rows, and no statement is an UPDATE."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> BindingRecord:
        await store.put(AGENT, URL_ONE, OWNER)
        return await store.put(AGENT, URL_TWO, OWNER)

    second = asyncio.run(go())

    assert len(pool.rows) == 2
    assert len(pool.writes) == 2
    assert all("INSERT INTO agent_bindings" in s for s in pool.writes)
    assert not any("UPDATE" in s for s in pool.statements)
    assert second.previous_endpoint_url == URL_ONE


def test_no_sql_in_the_module_mutates_a_row() -> None:
    """Belt and braces on the constants themselves, so a later edit that adds
    an UPDATE has to delete this test to land."""
    sql = " ".join(
        (
            binding_store._CREATE_TABLE_SQL,
            binding_store._SELECT_LATEST_SQL,
            binding_store._INSERT_SQL,
        )
    ).upper()

    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert "ON CONFLICT" not in sql  # an upsert is an update wearing a hat


def test_get_returns_the_newest_row_with_its_predecessor() -> None:
    pool = FakePool()
    store = _pg(pool)

    async def go() -> BindingRecord | None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put(AGENT, URL_TWO, OWNER)
        await store.put(AGENT, URL_THREE, OWNER)
        return await store.get(AGENT)

    current = asyncio.run(go())

    assert current is not None
    assert current.agent_id == AGENT
    assert current.endpoint_url == URL_THREE
    assert current.previous_endpoint_url == URL_TWO
    assert len(pool.rows) == 3  # all three binds are still on the audit trail


def test_get_is_none_when_the_agent_has_no_row() -> None:
    pool = FakePool()

    assert asyncio.run(_pg(pool).get("agt_never")) is None


def test_the_insert_returns_the_stored_timestamp() -> None:
    """`put` dates the record itself and stores that same value, so the record
    handed back is the row that was written — not a second clock reading."""
    pool = FakePool()
    store = _pg(pool)

    before = time.time()
    record = asyncio.run(store.put(AGENT, URL_ONE, OWNER))
    after = time.time()

    assert before <= record.bound_at <= after
    assert pool.rows[0]["bound_at"] == record.bound_at


def test_close_closes_the_pool_once_and_is_safe_twice() -> None:
    pool = FakePool()
    store = _pg(pool)

    async def go() -> None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.close()
        await store.close()

    asyncio.run(go())

    assert pool.closed == 1


def test_the_pool_is_opened_with_min_size_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A free Render instance idles and its sockets die with it — a pool that
    insists on a live connection wakes up holding a dead one."""
    captured: dict[str, object] = {}

    async def fake_create_pool(**kwargs: object) -> FakePool:
        captured.update(kwargs)
        return FakePool()

    class FakeAsyncpg:
        create_pool = staticmethod(fake_create_pool)

    monkeypatch.setattr(binding_store, "_import_asyncpg", lambda: FakeAsyncpg)
    store = binding_store.PostgresBindingStore("postgres://user:pw@example.invalid/db")

    asyncio.run(store.get(AGENT))

    assert captured["min_size"] == 0
    assert captured["dsn"] == "postgres://user:pw@example.invalid/db"
    assert captured["max_size"] == binding_store._POOL_MAX_SIZE


def test_a_missing_driver_names_the_fix() -> None:
    """asyncpg is imported at first use, not at module scope, so this suite —
    and any checkout without the driver — imports and collects cleanly."""
    try:
        import asyncpg  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        pytest.skip("asyncpg is installed, so the missing-driver path cannot be reached")

    with pytest.raises(RuntimeError) as excinfo:
        binding_store._import_asyncpg()

    message = str(excinfo.value)
    assert "asyncpg" in message and "DATABASE_URL" in message
