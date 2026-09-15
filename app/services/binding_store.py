"""
Durable storage for operator endpoint bindings (story 2.01, ADR 0003 D1).

Every other store in this service is process-local and says so —
`AppState` ("contents are lost on restart", app/state.py:14-22) and
`app/pdax/ramp_store.py` both name the database they never got. A binding
cannot join them: AC-5 is literally "the binding survives a backend restart",
and the free Render instance spins down after ~15 minutes idle and comes back
from the image, so a binding in this process (or on this process's filesystem)
is gone before anyone looks at it.

So this module is the first durable write the service makes, and it is shaped
to keep that from costing anything anywhere else:

  - `BindingStore` is a Protocol, and `database_url` picks the implementation
    at runtime. Empty — the default, and what tests and local dev run with —
    selects `InMemoryBindingStore`, so the suite stays hermetic and offline and
    the 82% coverage gate is unaffected. Production sets DATABASE_URL in the
    Render dashboard and gets Postgres with no code change.
  - The dependency is asyncpg and nothing else: no ORM and no migration
    framework, because this is one table. The driver is imported lazily so a
    checkout without it still boots, imports and tests cleanly.
  - The Postgres table is APPEND-ONLY. A rebind inserts a new row rather than
    updating one, so AC-6's timestamp and the audit history come for free and
    `previous_endpoint_url` is derived at read time rather than maintained.

`put` returns the NEW record with `previous_endpoint_url` already populated, so
a caller never has to read-then-write and AC-6 cannot race two rebinds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import settings

logger = logging.getLogger(__name__)

# Retention cap for the in-memory store, sized like ramp_store._MAX_RAMPS (500)
# so the process's bounded stores agree on what "bounded" costs.
_MAX_BINDINGS = 500

# Pool sizing for the Postgres store.
#
# min_size=0 is the load-bearing one. A free Render instance idles and is spun
# down, and its TCP sockets die with it; a pool that insists on keeping a live
# connection wakes up holding a dead one and hands it to the first request. At
# zero the pool holds nothing while nothing is happening and opens a fresh
# connection on demand, which is also what a serverless Postgres (Neon) wants.
#
# max_size is small because uvicorn runs --workers 1 (render.yaml): this is the
# whole service's connection budget, not one worker's share of it, and free-tier
# Postgres counts connections much more tightly than queries.
_POOL_MIN_SIZE = 0
_POOL_MAX_SIZE = 5

# Created on first use with CREATE TABLE IF NOT EXISTS. There is no migration
# tooling in this repo and one table does not justify introducing any: the DDL
# is idempotent, so every boot and every redeploy converges on the same schema
# with no migration step that could fail a deploy at 3am.
#
# The table is APPEND-ONLY — a rebind INSERTs a new row and nothing ever UPDATEs
# one — which is what makes AC-6 cheap: the timestamp is the row's own
# `bound_at`, the rebind history IS the audit trail, and `previous_endpoint_url`
# is a window function over that history rather than a second copy of the truth
# that can drift out of step with the first.
#
# Ordering is by the surrogate `id`, never by `bound_at`: two binds landing in
# the same clock tick must still have a defined newest, and a float timestamp
# cannot promise that. `bound_at` is stored as epoch seconds to match
# BindingRecord exactly (`to_timestamp(bound_at)` renders it for a human), so
# nothing converts a timezone between the write and the read.
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_bindings (
    id           BIGSERIAL PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    owner        TEXT NOT NULL,
    bound_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_bindings_agent_id_id_desc_idx
    ON agent_bindings (agent_id, id DESC);
"""

# The newest row for one agent, with the endpoint it replaced derived from the
# row before it. LAG runs over every row for the agent — window functions are
# evaluated before the outer ORDER BY/LIMIT — so the one row this returns
# already knows its own predecessor.
_SELECT_LATEST_SQL = """
SELECT endpoint_url, owner, bound_at, previous_endpoint_url
FROM (
    SELECT id,
           endpoint_url,
           owner,
           bound_at,
           LAG(endpoint_url) OVER (ORDER BY id) AS previous_endpoint_url
    FROM agent_bindings
    WHERE agent_id = $1
) AS history
ORDER BY id DESC
LIMIT 1
"""

# Append the new binding and return it with its predecessor, in ONE statement.
# Every CTE here sees the same snapshot, so `previous` cannot observe the row
# `inserted` writes, and there is no window between a read and a write for a
# second rebind to slip into — which is how `put` can promise a populated
# `previous_endpoint_url` without the caller doing a read-then-write.
# LEFT JOIN ... ON TRUE keeps the first-ever bind (no predecessor) returning a
# row with previous_endpoint_url NULL rather than returning nothing at all.
_INSERT_SQL = """
WITH previous AS (
    SELECT endpoint_url
    FROM agent_bindings
    WHERE agent_id = $1
    ORDER BY id DESC
    LIMIT 1
), inserted AS (
    INSERT INTO agent_bindings (agent_id, endpoint_url, owner, bound_at)
    VALUES ($1, $2, $3, $4)
    RETURNING endpoint_url, owner, bound_at
)
SELECT inserted.endpoint_url,
       inserted.owner,
       inserted.bound_at,
       previous.endpoint_url AS previous_endpoint_url
FROM inserted
LEFT JOIN previous ON TRUE
"""


def _import_asyncpg() -> Any:
    """Import the driver at first Postgres use, never at module import.

    This module is imported on every boot — by the router, and so by the
    hermetic suite and by any checkout that installed only the dev
    requirements. asyncpg is needed solely when DATABASE_URL is set, so
    importing it at module scope would turn an optional dependency into a
    mandatory one and break test collection wherever it is absent. Deferring it
    means a missing driver surfaces here, at the moment something actually
    wanted a database, with the fix in the message instead of as an ImportError
    from an unrelated module three imports away.
    """
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "DATABASE_URL is set but asyncpg is not installed, so bindings cannot be "
            "stored durably. Install it (`pip install -r requirements.txt`, asyncpg>=0.30,<1) "
            "or clear DATABASE_URL to fall back to the in-memory store."
        ) from exc
    return asyncpg


@dataclass(frozen=True)
class BindingRecord:
    """One (agent_id -> endpoint_url) binding, as proved and stored.

    Frozen because a record is evidence, not state: it is what the store
    accepted at `bound_at`, and the way to change a binding is to make another
    one, not to mutate this.
    """

    agent_id: str
    endpoint_url: str
    # The G-address that proved ownership at bind time — recorded, not
    # re-derived, so the audit trail survives a later ownership change.
    owner: str
    # Epoch seconds, matching the house style for timestamps (app/schemas.py:70)
    # rather than a datetime, so no timezone conversion sits between what was
    # stored and what AC-6 reports.
    bound_at: float
    # The endpoint this binding replaced, or None on a first bind. Derived from
    # the row history, never maintained as its own piece of mutable state.
    previous_endpoint_url: str | None


class BindingStore(Protocol):
    """The seam between the router and wherever bindings actually live.

    Narrow on purpose: three methods, all awaitable even in the in-memory
    implementation that needs none of it, so swapping in Postgres is a
    configuration change rather than a rewrite of every call site.
    """

    async def get(self, agent_id: str) -> BindingRecord | None:
        """The current binding for `agent_id`, or None if it has never bound."""
        ...

    async def put(self, agent_id: str, endpoint_url: str, owner: str) -> BindingRecord:
        """Record a binding and return it, `previous_endpoint_url` populated."""
        ...

    async def close(self) -> None:
        """Release anything held (a connection pool); safe to call twice."""
        ...


class InMemoryBindingStore:
    """Bindings for the lifetime of the process — the DATABASE_URL-less default.

    This is what local dev and the hermetic suite run on, and it does NOT
    satisfy AC-5: a restart loses everything here, which is the whole reason
    `PostgresBindingStore` exists. It is still the right default, because the
    alternative is a suite and a laptop that need a database to boot.

    Retention is bounded the way `app/pdax/ramp_store.py` bounds ramps and
    `app/state.py` bounds tasks: an insertion-ordered map with a hard cap, and
    an EXPLICIT eviction on the insert that would exceed it — never an
    unbounded dict that a long-lived process grows forever. Two deliberate
    differences from ramp_store, both forced by what a binding is:

      - There is no terminal-first tier. A ramp can be `completed`/`failed` and
        therefore cheap to drop; a binding is live until something replaces it,
        so every candidate costs the same and the victim is simply the oldest.
      - A rebind refreshes the entry's position (`move_to_end`), so "oldest"
        means least recently bound rather than first ever seen. Losing the
        binding nobody has touched is the least-bad loss available.

    Eviction is logged at WARNING for ramp_store's reason: the operator's
    endpoint silently disappearing — requiring a re-bind nobody was told about
    — is exactly the failure that must not happen quietly.
    """

    def __init__(self, max_bindings: int = _MAX_BINDINGS) -> None:
        self._max_bindings = max_bindings
        self._bindings: OrderedDict[str, BindingRecord] = OrderedDict()

    async def get(self, agent_id: str) -> BindingRecord | None:
        return self._bindings.get(agent_id)

    async def put(self, agent_id: str, endpoint_url: str, owner: str) -> BindingRecord:
        previous = self._bindings.get(agent_id)
        # Only a NEW agent id can grow the store, so a rebind never evicts.
        if previous is None and len(self._bindings) >= self._max_bindings:
            self._evict_one()
        record = BindingRecord(
            agent_id=agent_id,
            endpoint_url=endpoint_url,
            owner=owner,
            bound_at=time.time(),
            previous_endpoint_url=previous.endpoint_url if previous is not None else None,
        )
        self._bindings[agent_id] = record
        self._bindings.move_to_end(agent_id)
        return record

    def _evict_one(self) -> None:
        """Drop the least recently bound agent so the cap can never be exceeded."""
        victim, evicted = self._bindings.popitem(last=False)
        logger.warning(
            "evicted in-memory binding at cap: agent_id=%s endpoint=%s bound_at=%s cap=%d "
            "(set DATABASE_URL to store bindings durably)",
            victim,
            evicted.endpoint_url,
            evicted.bound_at,
            self._max_bindings,
        )

    async def close(self) -> None:
        """Drop every record.

        There is no pool to release, but close() must mean the same thing for
        both stores — after it, this one holds nothing and is safe to call
        again — rather than quietly being a no-op on one side of the Protocol.
        """
        self._bindings.clear()


class PostgresBindingStore:
    """Append-only bindings in Postgres — the implementation AC-5 needs.

    Deliberately thin: asyncpg, three SQL constants, and no ORM or migration
    framework, because there is exactly one table and a dependency that has to
    be understood before a deploy can be debugged is worse than the SQL it
    replaces.

    The pool and the table are both created LAZILY, on the first call that
    needs them, so constructing the store never does I/O — importing this
    module, resolving the singleton and booting the app all stay offline, and a
    database that is briefly unreachable at boot costs a failed request rather
    than a failed deploy.

    `pool` is injectable for exactly one reason and it is stated rather than
    disguised: the test suite is hermetic and has no database, so it passes a
    fake pool and asserts the SQL this class actually sends.
    """

    def __init__(self, dsn: str, *, pool: Any | None = None) -> None:
        self._dsn = dsn
        self._pool: Any | None = pool
        # Whether the DDL has been run against THIS pool. Separate from the
        # pool itself so close() can reset both and a later call rebuilds them.
        self._ready = False
        # Serializes first use: a burst of concurrent binds on a cold process
        # must create one pool and run the DDL once, not one per request.
        self._lock = asyncio.Lock()

    async def _ready_pool(self) -> Any:
        if self._ready and self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await self._create_pool()
            if not self._ready:
                # asyncpg runs argument-less queries through the simple
                # protocol, which is what lets one execute() carry both DDL
                # statements.
                await self._pool.execute(_CREATE_TABLE_SQL)
                self._ready = True
        return self._pool

    async def _create_pool(self) -> Any:
        asyncpg = _import_asyncpg()
        return await asyncpg.create_pool(dsn=self._dsn, min_size=_POOL_MIN_SIZE, max_size=_POOL_MAX_SIZE)

    async def get(self, agent_id: str) -> BindingRecord | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_LATEST_SQL, agent_id)
        return None if row is None else self._to_record(agent_id, row)

    async def put(self, agent_id: str, endpoint_url: str, owner: str) -> BindingRecord:
        pool = await self._ready_pool()
        # Timestamped here rather than with the database's now(): the record
        # handed back must be the row that was stored, and this service already
        # dates everything by its own clock in epoch seconds.
        bound_at = time.time()
        row = await pool.fetchrow(_INSERT_SQL, agent_id, endpoint_url, owner, bound_at)
        if row is None:  # pragma: no cover — the CTE always returns the inserted row
            raise RuntimeError(f"binding insert returned no row for agent_id={agent_id}")
        return self._to_record(agent_id, row)

    @staticmethod
    def _to_record(agent_id: str, row: Any) -> BindingRecord:
        """Map one asyncpg Record to a BindingRecord.

        `agent_id` comes from the caller, not the row: it is the key both
        queries filtered on, so selecting it back would be a column of round
        trip bought for nothing.
        """
        return BindingRecord(
            agent_id=agent_id,
            endpoint_url=row["endpoint_url"],
            owner=row["owner"],
            bound_at=float(row["bound_at"]),
            previous_endpoint_url=row["previous_endpoint_url"],
        )

    async def close(self) -> None:
        # Cleared before the await so a close racing a request cannot hand out
        # the pool that is being torn down, and so a second close is a no-op.
        pool, self._pool = self._pool, None
        self._ready = False
        if pool is not None:
            await pool.close()


# The process-wide store. A module-level singleton and NOT @lru_cache, which is
# the obvious shortcut and is wrong here in a way that is invisible until
# production: lru_cache would freeze whichever store the FIRST import happened
# to resolve, so a DATABASE_URL that arrives later — set in the Render
# dashboard, or by a test, or by anything that configures settings after this
# module is imported — would be silently ignored and the service would keep
# writing to memory while reporting success. That is precisely the failure this
# abstraction exists to prevent, so the singleton is explicit and
# close_binding_store() can clear it.
_store: BindingStore | None = None


def get_binding_store() -> BindingStore:
    """The process's binding store, built on first use from `database_url`.

    Resolved lazily — at the first call, not at import — so the value read is
    the configuration the process is actually running with.
    """
    global _store
    if _store is None:
        dsn = settings.database_url.strip()
        if dsn:
            _store = PostgresBindingStore(dsn)
            # The DSN carries the database password: report the choice, never
            # the value.
            logger.info("binding store: postgres (DATABASE_URL is set) — bindings survive a restart")
        else:
            _store = InMemoryBindingStore()
            logger.info(
                "binding store: in-memory (DATABASE_URL is unset) — bindings are LOST on restart; "
                "set DATABASE_URL to persist them"
            )
    return _store


async def close_binding_store() -> None:
    """Release the store at lifespan shutdown; safe to call twice.

    The singleton is cleared as well as closed, so a process that shuts a store
    down and keeps running re-reads `database_url` next time instead of handing
    out a closed pool.
    """
    global _store
    store, _store = _store, None
    if store is not None:
        await store.close()
