"""
Durable copies of read snapshots, so a restart does not start them from nothing.

The free Render instance sleeps after ~15 minutes idle and boots from the image
on the next request, so in practice EVERY reviewer visit after a quiet spell is
the first request of a new process. A snapshot that takes minutes to build (the
adoption report, D-091) would then answer "computing" to nearly every visit. So
each live build of an opted-in cell is written to Postgres, and the next
process serves the last one — marked `X-Snapshot-Source: persisted`, dated by
`generated_at` and its age — until its own first build replaces it.

A cache, not a record: one row per (name, scope), overwritten in place, and a
row that cannot be read back (a schema the model no longer accepts, a scope
that is not this deployment's) is ignored rather than served. `scope` binds a
row to the network and contracts it was computed against, so a testnet report
can never be restored into a mainnet process, nor one for a since-redeployed
registry.

Like the other stores here: asyncpg only, imported lazily, a pool that holds
nothing while idle (min_size=0, which is what a sleeping instance and Neon
want), idempotent DDL on first use in place of a migration tool, and the
in-memory implementation whenever DATABASE_URL is unset (tests, local dev).
Every failure is logged and swallowed: persistence is an optimisation of the
cold path, and must never be why a snapshot is not served.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any, NamedTuple, Protocol, TypeVar

from ..config import settings
from . import snapshots
from .pg_schema import create_schema
from .snapshots import Snapshot, SnapshotCell

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One connection is plenty: a write per rebuild (minutes apart) and a read per
# boot. Kept out of the dispute and binding stores' pools so a slow snapshot
# write can never queue a settlement behind it.
_POOL_MIN_SIZE = 0
_POOL_MAX_SIZE = 1
_POOL_CONNECT_TIMEOUT = 10.0
_POOL_COMMAND_TIMEOUT = 10.0

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS read_snapshots (
    name         TEXT NOT NULL,
    scope        TEXT NOT NULL,
    generated_at DOUBLE PRECISION NOT NULL,
    body         TEXT NOT NULL,
    saved_at     DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (name, scope)
)
"""

# Never moves a row backwards: a slow write of an older build landing after a
# newer one must not replace it.
_UPSERT_SQL = """
INSERT INTO read_snapshots (name, scope, generated_at, body, saved_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (name, scope) DO UPDATE
SET generated_at = EXCLUDED.generated_at, body = EXCLUDED.body, saved_at = EXCLUDED.saved_at
WHERE read_snapshots.generated_at <= EXCLUDED.generated_at
"""

_SELECT_SQL = "SELECT generated_at, body FROM read_snapshots WHERE name = $1 AND scope = $2"


class StoredSnapshot(NamedTuple):
    generated_at: float
    body: bytes


class SnapshotStore(Protocol):
    async def load(self, name: str, scope: str) -> StoredSnapshot | None: ...

    async def save(self, name: str, scope: str, generated_at: float, body: bytes) -> None: ...

    async def close(self) -> None: ...


class InMemorySnapshotStore:
    """The store when DATABASE_URL is unset: lives and dies with the process."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], StoredSnapshot] = {}

    async def load(self, name: str, scope: str) -> StoredSnapshot | None:
        return self._rows.get((name, scope))

    async def save(self, name: str, scope: str, generated_at: float, body: bytes) -> None:
        held = self._rows.get((name, scope))
        if held is None or held.generated_at <= generated_at:
            self._rows[(name, scope)] = StoredSnapshot(generated_at, body)

    async def close(self) -> None:
        return None


class PostgresSnapshotStore:
    """One table, one row per snapshot, created on first use."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ready_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                import asyncpg  # lazily: only a deployment with DATABASE_URL needs the driver

                pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                    timeout=_POOL_CONNECT_TIMEOUT,
                    command_timeout=_POOL_COMMAND_TIMEOUT,
                )
                try:
                    await create_schema(pool, "read_snapshots", _CREATE_TABLE_SQL, timeout=_POOL_COMMAND_TIMEOUT)
                except BaseException:
                    await pool.close()
                    raise
                self._pool = pool
            return self._pool

    async def load(self, name: str, scope: str) -> StoredSnapshot | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_SQL, name, scope, timeout=_POOL_COMMAND_TIMEOUT)
        if row is None:
            return None
        return StoredSnapshot(float(row["generated_at"]), str(row["body"]).encode())

    async def save(self, name: str, scope: str, generated_at: float, body: bytes) -> None:
        pool = await self._ready_pool()
        await pool.execute(
            _UPSERT_SQL, name, scope, generated_at, body.decode(), time.time(), timeout=_POOL_COMMAND_TIMEOUT
        )

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()


_store: SnapshotStore | None = None


def get_snapshot_store() -> SnapshotStore:
    """The process's store, chosen from DATABASE_URL at first use."""
    global _store
    if _store is None:
        _store = PostgresSnapshotStore(settings.database_url) if settings.database_url else InMemorySnapshotStore()
    return _store


_saves: set[asyncio.Task[None]] = set()


async def close_snapshot_store() -> None:
    """Finish (briefly) the writes in flight and release the pool (shutdown)."""
    global _store
    pending = [t for t in _saves if not t.done()]
    if pending:
        _done, late = await asyncio.wait(pending, timeout=5)
        for task in late:
            task.cancel()
    store, _store = _store, None
    if store is not None:
        try:
            await store.close()
        except Exception as e:
            logger.warning("snapshot store did not close cleanly: %s", e)


def deployment_scope() -> str:
    """The network and contracts a snapshot was computed against, hashed."""
    parts = (
        settings.stellar_network_passphrase,
        settings.stellar_agent_registry,
        settings.stellar_payment_escrow,
        settings.stellar_reputation_ledger,
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def persist(cell: SnapshotCell[T], decode: Callable[[bytes], T], *, max_restore_age_seconds: float | None) -> None:
    """Save every live build of `cell`, and restore the last one at boot.

    A restored snapshot is served only until the cell's first build of this
    process lands, and never when it is older than `max_restore_age_seconds`:
    past that, "computing" is the more honest answer than a figure that old.
    None restores one of any age — for a value whose response carries its age
    and that is better shown dated than not at all.
    """

    def save(snap: Snapshot[T]) -> None:
        task = asyncio.get_running_loop().create_task(_save(cell.name, snap))
        _saves.add(task)
        task.add_done_callback(_saves.discard)

    async def restore() -> None:
        await _restore(cell, decode, max_restore_age_seconds)

    restore.__name__ = f"restore_{cell.name}_snapshot"
    cell.on_stored(save)
    snapshots.on_boot(restore)


async def _save(name: str, snap: Snapshot[Any]) -> None:
    try:
        await get_snapshot_store().save(name, deployment_scope(), snap.generated_at, snap.body)
    except Exception as e:
        logger.warning("snapshot %s not persisted: %s: %s", name, type(e).__name__, e)


async def _restore(cell: SnapshotCell[T], decode: Callable[[bytes], T], max_age: float | None) -> None:
    try:
        stored = await get_snapshot_store().load(cell.name, deployment_scope())
    except Exception as e:
        logger.warning("snapshot %s not restored: %s: %s", cell.name, type(e).__name__, e)
        return
    if stored is None:
        return
    age = time.time() - stored.generated_at
    if max_age is not None and age > max_age:
        logger.info("snapshot %s not restored: the stored one is %.0f s old", cell.name, age)
        return
    try:
        value = decode(stored.body)
    except Exception as e:
        logger.warning("snapshot %s not restored: the stored body no longer decodes: %s", cell.name, e)
        return
    if cell.restore(value, stored.generated_at):
        logger.info("snapshot %s restored from storage, %.0f s old", cell.name, age)
