"""Story 2.01 — the operator endpoint binding store (ADR 0003 D1).

Four things are asserted here and they are the four that can silently rot:

  1. The in-memory default is BOUNDED, like every other store in this process
     (app/state.py's deque(maxlen=200), ramp_store's _MAX_RAMPS=500) — a store
     the whole internet can write to, one row per bind, cannot be a plain dict.
  2. The Postgres table is APPEND-ONLY. A rebind INSERTs, an unbind INSERTs a
     tombstone; nothing UPDATEs and nothing DELETEs. That is what makes AC-6's
     timestamp and `previous_endpoint_url` derivable rather than maintained, so
     the SQL itself is under test.
  3. A REVOKED binding is gone from both read paths — `get` answers None and
     `list_agent_ids` omits the id — while every row of its history survives.
     A revocation that a restart could undo is not a revocation.
  4. The store is chosen from `database_url` at first USE, not at import, so a
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

from app.services import binding_registry, binding_store
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


# ── revoking a binding, in memory ─────────────────────────────────────────


def test_delete_revokes_the_binding_and_reports_what_it_revoked() -> None:
    store = InMemoryBindingStore()

    async def go() -> tuple[binding_store.UnbindRecord | None, BindingRecord | None]:
        await store.put(AGENT, URL_ONE, OWNER)
        return await store.delete(AGENT, OWNER), await store.get(AGENT)

    before = time.time()
    record, current = asyncio.run(go())
    after = time.time()

    assert record is not None
    assert record.agent_id == AGENT
    assert record.revoked_endpoint_url == URL_ONE
    assert record.owner == OWNER
    # 2.01 AC-6 asks for the change to be recorded WITH A TIMESTAMP, and a
    # revocation is a change.
    assert before <= record.unbound_at <= after
    assert current is None  # nothing is dispatched here any more


def test_deleting_an_agent_that_is_not_bound_is_not_an_error() -> None:
    """The caller is asking for an end state, and the end state already holds.
    Twice in a row is the same story: the route answers 200 either way."""
    store = InMemoryBindingStore()

    async def go() -> tuple[object, object, object]:
        never = await store.delete("agt_never", OWNER)
        await store.put(AGENT, URL_ONE, OWNER)
        first = await store.delete(AGENT, OWNER)
        second = await store.delete(AGENT, OWNER)
        return never, first, second

    never, first, second = asyncio.run(go())

    assert never is None
    assert first is not None
    assert second is None


def test_a_revoked_agent_is_not_in_list_agent_ids() -> None:
    """The set that seeds the planner's routability filter at startup. An agent
    that stayed in it would be re-offered by the next boot, which is the whole
    failure a revocation exists to prevent."""
    store = InMemoryBindingStore()

    async def go() -> tuple[frozenset[str], frozenset[str]]:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put("agt_other", URL_TWO, OWNER)
        before = await store.list_agent_ids()
        await store.delete(AGENT, OWNER)
        return before, await store.list_agent_ids()

    before, after = asyncio.run(go())

    assert before == frozenset({AGENT, "agt_other"})
    assert after == frozenset({"agt_other"})


def test_binding_again_after_a_revocation_replaces_nothing() -> None:
    """`replaced` is how the API reports "your old endpoint is gone". After a
    revocation there was no old endpoint, and saying otherwise would read as
    the unbind having silently not taken effect."""
    store = InMemoryBindingStore()

    async def go() -> BindingRecord:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.delete(AGENT, OWNER)
        return await store.put(AGENT, URL_TWO, OWNER)

    assert asyncio.run(go()).previous_endpoint_url is None


def test_eviction_removes_the_id_from_the_planner_s_routable_set() -> None:
    """The desync this store could cause on its own. Eviction drops a binding
    the planner's in-memory set still believes in, so the planner keeps
    offering the agent, `resolve_worker` finds no binding, and the step is
    silently skipped — a plan that quietly does less than it says."""
    store = InMemoryBindingStore(max_bindings=1)
    binding_registry.note_bound("ext_evicted")
    try:
        assert binding_registry.is_dispatchable("ext_evicted") is True

        async def go() -> None:
            await store.put("ext_evicted", URL_ONE, OWNER)
            await store.put("ext_survivor", URL_TWO, OWNER)  # at cap → drops ext_evicted

        asyncio.run(go())

        assert asyncio.run(store.get("ext_evicted")) is None
        assert binding_registry.is_dispatchable("ext_evicted") is False
    finally:
        binding_registry.note_unbound("ext_evicted")


# ── PostgresBindingStore ──────────────────────────────────────────────────


class FakePool:
    """Stands in for an asyncpg pool: records every statement and models the
    one table just well enough to answer the queries the store sends.

    Rows are kept in a plain list, and list order IS the BIGSERIAL `id` order
    the real queries sort by — so "the newest row" means the same thing here as
    it does in Postgres, and an implementation that started UPDATEing rows
    instead of appending them would visibly change `rows`.

    Statements are dispatched by EQUALITY against the module's own constants,
    never by sniffing for a substring. Two of the four statements now contain
    both `INSERT` and `revoked`, so substring matching would silently route a
    tombstone into the bind branch and the tests would keep passing while
    asserting the wrong thing; equality means a query this fake has not been
    taught fails loudly here instead.
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
        if sql == binding_store._INSERT_SQL:
            return self._insert(*args)
        if sql == binding_store._TOMBSTONE_SQL:
            return self._tombstone(*args)
        assert sql == binding_store._SELECT_LATEST_SQL, f"unexpected statement: {sql}"
        return self._latest(str(args[0]))

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        self.statements.append(sql)
        assert sql == binding_store._SELECT_AGENT_IDS_SQL, f"unexpected statement: {sql}"
        # DISTINCT ON (agent_id) ... ORDER BY agent_id, id DESC, then drop the
        # tombstones: last write per agent wins, and a revoked agent is absent.
        newest = {str(r["agent_id"]): r for r in self.rows}
        return [{"agent_id": a} for a, row in newest.items() if not row["revoked"]]

    def _insert(self, *args: object) -> dict[str, object]:
        agent_id, endpoint_url, owner, bound_at = args
        previous = self._live_endpoint(str(agent_id))
        self.rows.append(
            {
                "agent_id": agent_id,
                "endpoint_url": endpoint_url,
                "owner": owner,
                "bound_at": bound_at,
                "revoked": False,
            }
        )
        return {
            "endpoint_url": endpoint_url,
            "owner": owner,
            "bound_at": bound_at,
            "previous_endpoint_url": previous,
        }

    def _tombstone(self, *args: object) -> dict[str, object] | None:
        agent_id, owner, bound_at = args
        history = self._history(str(agent_id))
        # `INSERT ... SELECT FROM live WHERE NOT live.revoked`: with nothing
        # live there is no row to select, so nothing is written and nothing
        # comes back.
        if not history or history[-1]["revoked"]:
            return None
        endpoint_url = history[-1]["endpoint_url"]
        self.rows.append(
            {
                "agent_id": agent_id,
                "endpoint_url": endpoint_url,
                "owner": owner,
                "bound_at": bound_at,
                "revoked": True,
            }
        )
        return {"endpoint_url": endpoint_url, "owner": owner, "bound_at": bound_at}

    def _latest(self, agent_id: str) -> dict[str, object] | None:
        history = self._history(agent_id)
        if not history:
            return None
        newest = history[-1]
        previous = history[-2] if len(history) > 1 else None
        return {
            "endpoint_url": newest["endpoint_url"],
            "owner": newest["owner"],
            "bound_at": newest["bound_at"],
            # LAG(CASE WHEN revoked THEN NULL ELSE endpoint_url END)
            "previous_endpoint_url": None if previous is None or previous["revoked"] else previous["endpoint_url"],
            "revoked": newest["revoked"],
        }

    def _live_endpoint(self, agent_id: str) -> object | None:
        """The `previous` CTE: the newest row's endpoint, or None when that row
        is a tombstone — binding again after a revocation replaces nothing."""
        history = self._history(agent_id)
        if not history or history[-1]["revoked"]:
            return None
        return history[-1]["endpoint_url"]

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
            binding_store._SELECT_AGENT_IDS_SQL,
            binding_store._INSERT_SQL,
            binding_store._TOMBSTONE_SQL,
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


# ── get_binding_store / close_binding_store ───────────────────────────────

DSN = "postgres://user:sup3rsecret@db.example.invalid/orizon"


@pytest.fixture(autouse=True)
def reset_singleton():
    """The resolver is a module-level singleton; no test may inherit another's."""
    binding_store._store = None
    yield
    binding_store._store = None


def test_an_empty_database_url_selects_the_in_memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binding_store.settings, "database_url", "")

    assert isinstance(binding_store.get_binding_store(), InMemoryBindingStore)


def test_a_database_url_selects_postgres_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving must not do I/O: a database that is briefly unreachable at
    boot has to cost a failed request, not a failed deploy."""
    monkeypatch.setattr(binding_store.settings, "database_url", DSN)

    store = binding_store.get_binding_store()

    assert isinstance(store, binding_store.PostgresBindingStore)
    assert store._pool is None  # nothing dialled yet


def test_the_store_is_resolved_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binding_store.settings, "database_url", "")

    assert binding_store.get_binding_store() is binding_store.get_binding_store()


def test_the_resolver_is_not_lru_cached() -> None:
    """ADR 0003 D1 forbids @lru_cache here, and this is the assertion that
    keeps someone from "tidying" the global away: a cached resolver would pin
    whichever store the first import resolved and silently ignore a later
    DATABASE_URL — writing to memory while reporting success, which is exactly
    the failure the abstraction exists to prevent."""
    assert not hasattr(binding_store.get_binding_store, "cache_clear")
    assert not hasattr(binding_store.get_binding_store, "cache_info")


def test_a_database_url_that_arrives_later_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half of the test above."""
    monkeypatch.setattr(binding_store.settings, "database_url", "")
    assert isinstance(binding_store.get_binding_store(), InMemoryBindingStore)

    monkeypatch.setattr(binding_store.settings, "database_url", DSN)
    asyncio.run(binding_store.close_binding_store())

    assert isinstance(binding_store.get_binding_store(), binding_store.PostgresBindingStore)


def test_close_releases_the_store_and_clears_the_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binding_store.settings, "database_url", "")
    first = binding_store.get_binding_store()
    asyncio.run(first.put(AGENT, URL_ONE, OWNER))

    asyncio.run(binding_store.close_binding_store())
    second = binding_store.get_binding_store()

    assert second is not first
    assert asyncio.run(second.get(AGENT)) is None


def test_close_is_a_no_op_when_nothing_was_ever_resolved() -> None:
    """Lifespan shutdown calls this unconditionally, including on a process
    that never served a bind."""
    asyncio.run(binding_store.close_binding_store())

    assert binding_store._store is None


def test_the_choice_is_logged_but_the_dsn_never_is(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A DSN carries the database password; the log says which store was
    chosen and nothing else."""
    monkeypatch.setattr(binding_store.settings, "database_url", DSN)

    with caplog.at_level(logging.INFO, logger=STORE_LOGGER):
        binding_store.get_binding_store()

    lines = _messages(caplog, STORE_LOGGER)
    assert any("postgres" in m for m in lines)
    assert not any("sup3rsecret" in m or DSN in m for m in lines)


def test_the_in_memory_default_announces_that_it_loses_bindings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-5 is false on this path, so a deployment running it must be able to
    find that out from its own startup log rather than from a lost binding."""
    monkeypatch.setattr(binding_store.settings, "database_url", "")

    with caplog.at_level(logging.INFO, logger=STORE_LOGGER):
        binding_store.get_binding_store()

    lines = _messages(caplog, STORE_LOGGER)
    assert any("in-memory" in m and "LOST on restart" in m for m in lines)


# ── revoking a binding, in Postgres ───────────────────────────────────────


def test_an_unbind_appends_a_tombstone_and_deletes_nothing() -> None:
    """The append-only rule at its most tempting breaking point: the obvious
    implementation of "unbind" is `DELETE FROM`, and that would destroy exactly
    the evidence an operator needs after a host compromise."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> binding_store.UnbindRecord | None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put(AGENT, URL_TWO, OWNER)
        return await store.delete(AGENT, OWNER)

    record = asyncio.run(go())

    assert record is not None
    assert record.revoked_endpoint_url == URL_TWO
    # Two binds and a revocation — three rows, and the first two still say what
    # they always said.
    assert len(pool.rows) == 3
    assert [r["endpoint_url"] for r in pool.rows] == [URL_ONE, URL_TWO, URL_TWO]
    assert [r["revoked"] for r in pool.rows] == [False, False, True]
    assert all("INSERT INTO agent_bindings" in s for s in pool.writes)
    assert not any("DELETE" in s or "UPDATE" in s for s in pool.statements)


def test_the_tombstone_carries_the_timestamp_the_store_returned() -> None:
    """AC-6 again: the row that was written IS the record handed back, dated by
    this process's clock in epoch seconds."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> binding_store.UnbindRecord | None:
        await store.put(AGENT, URL_ONE, OWNER)
        return await store.delete(AGENT, "G" + "B" * 55)

    before = time.time()
    record = asyncio.run(go())
    after = time.time()

    assert record is not None
    assert before <= record.unbound_at <= after
    assert pool.rows[-1]["bound_at"] == record.unbound_at
    # The revoker is recorded, not the address that proved the original bind:
    # an agent can change hands, and the audit trail should say who did this.
    assert pool.rows[-1]["owner"] == "G" + "B" * 55
    assert pool.rows[0]["owner"] == OWNER


def test_get_is_none_once_the_newest_row_is_a_tombstone() -> None:
    """A `WHERE NOT revoked` would have selected the binding UNDERNEATH the
    tombstone and resurrected it, which is why the filter is not in the SQL."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> BindingRecord | None:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put(AGENT, URL_TWO, OWNER)
        await store.delete(AGENT, OWNER)
        return await store.get(AGENT)

    assert asyncio.run(go()) is None


def test_list_agent_ids_excludes_a_tombstoned_agent() -> None:
    """A plain `SELECT DISTINCT agent_id` would list an agent forever once it
    had ever bound — the restart that undoes a revocation."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[frozenset[str], frozenset[str]]:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.put("agt_other", URL_TWO, OWNER)
        before = await store.list_agent_ids()
        await store.delete(AGENT, OWNER)
        return before, await store.list_agent_ids()

    before, after = asyncio.run(go())

    assert before == frozenset({AGENT, "agt_other"})
    assert after == frozenset({"agt_other"})


def test_unbinding_twice_writes_only_one_tombstone() -> None:
    """`INSERT ... SELECT FROM live WHERE NOT live.revoked` writes nothing when
    there is nothing live, so a client retrying a revocation — or a nightly
    script revoking a list — cannot grow the table one row per attempt."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[object, object]:
        await store.put(AGENT, URL_ONE, OWNER)
        return await store.delete(AGENT, OWNER), await store.delete(AGENT, OWNER)

    first, second = asyncio.run(go())

    assert first is not None
    assert second is None
    assert len(pool.rows) == 2  # the bind and one tombstone


def test_revoking_an_agent_that_never_bound_writes_nothing() -> None:
    pool = FakePool()

    assert asyncio.run(_pg(pool).delete("agt_never", OWNER)) is None
    assert pool.rows == []


def test_a_bind_after_a_tombstone_reports_no_previous_endpoint() -> None:
    """The LAG masks a tombstone, so the history still holds every row while
    `replaced` reports the truth: this bind replaced nothing."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[BindingRecord, BindingRecord | None]:
        await store.put(AGENT, URL_ONE, OWNER)
        await store.delete(AGENT, OWNER)
        rebound = await store.put(AGENT, URL_THREE, OWNER)
        return rebound, await store.get(AGENT)

    rebound, current = asyncio.run(go())

    assert rebound.previous_endpoint_url is None
    assert current is not None
    assert current.endpoint_url == URL_THREE
    assert current.previous_endpoint_url is None
    assert len(pool.rows) == 3  # the whole history survives the round trip
