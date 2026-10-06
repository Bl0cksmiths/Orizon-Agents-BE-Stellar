"""Create a store's tables on first use, safely when several processes do it at once.

Every Postgres store here creates its own schema with `CREATE ... IF NOT
EXISTS` the first time it is used, in place of a migration tool. That
statement is not safe against itself: two sessions creating the same table or
index at the same moment can both pass the existence check, and the loser
fails on the catalog's unique index (`duplicate key value violates unique
constraint "pg_type_typname_nsp_index"`). Two processes first touching a store
together — the old and the new instance across a deploy, or two workers —
do exactly that, and the request that lost the race fails.

`create_schema` runs a store's DDL in one transaction behind a
transaction-scoped advisory lock named for the store, so concurrent first uses
queue: the first creates everything, the rest find it there. The lock is
released when the transaction ends, so nothing outlives the call.
"""

from __future__ import annotations

from typing import Any

# hashtext() turns the store's name into the lock's bigint key in the database,
# so keys are readable at the call site and never collide by hand-picked number.
DDL_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1))"


def lock_name(store: str) -> str:
    """The advisory lock's name for one store's DDL."""
    return f"orizon.schema.{store}"


async def create_schema(
    pool: Any,
    store: str,
    *statements: str,
    timeout: float | None = None,
    acquire_timeout: float | None = None,
) -> None:
    """Run `statements` (argument-less DDL) in one transaction under `store`'s DDL lock.

    `timeout` bounds each statement — the lock wait included, so a process
    queued behind another's DDL gives up rather than hangs — and
    `acquire_timeout` (default: `timeout`) the wait for a connection.
    """
    wait = timeout if acquire_timeout is None else acquire_timeout
    async with pool.acquire(timeout=wait) as conn, conn.transaction():
        await conn.execute(DDL_LOCK_SQL, lock_name(store), timeout=timeout)
        for statement in statements:
            await conn.execute(statement, timeout=timeout)
