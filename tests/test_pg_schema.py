"""create_schema: a store's DDL in one transaction, behind its own advisory lock.

The race it closes is exercised against a real Postgres by each store's
`test_concurrent_first_uses_create_the_schema_without_a_race`; this pins the
shape — lock first, then the statements, all on one connection inside one
transaction, every wait bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from app.services import pg_schema


class Conn:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[object, ...], float | None]] = []
        self.acquired: list[float | None] = []
        self.in_transaction = False

    @contextlib.asynccontextmanager
    async def acquire(self, *, timeout: float | None = None) -> AsyncIterator[Conn]:
        self.acquired.append(timeout)
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.in_transaction = True
        yield
        self.in_transaction = False

    async def execute(self, sql: str, *args: object, timeout: float | None = None) -> None:
        assert self.in_transaction, "DDL ran outside the transaction its lock is scoped to"
        self.log.append((sql, args, timeout))


def test_the_lock_comes_first_and_everything_runs_in_one_transaction() -> None:
    conn = Conn()
    asyncio.run(pg_schema.create_schema(conn, "things", "CREATE A", "CREATE B", timeout=5.0, acquire_timeout=15.0))
    assert conn.log == [
        (pg_schema.DDL_LOCK_SQL, ("orizon.schema.things",), 5.0),
        ("CREATE A", (), 5.0),
        ("CREATE B", (), 5.0),
    ]
    assert conn.acquired == [15.0]


def test_the_connection_wait_defaults_to_the_statement_bound() -> None:
    conn = Conn()
    asyncio.run(pg_schema.create_schema(conn, "things", "CREATE A", timeout=5.0))
    assert conn.acquired == [5.0]
