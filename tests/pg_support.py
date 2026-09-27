"""Helpers for the tests that run the dispute store against a real Postgres.

The database itself is `pg_dsn` in tests/conftest.py: a DSN whose every
connection lands in a schema of the test's own. What is here is the plumbing
around it that more than one test file needs.

`run` exists because an asyncpg pool belongs to the event loop that dialled
it, and this suite's idiom is `asyncio.run` — one loop per call. A store used
across two `asyncio.run`s would hand the second loop a pool the first one owns.
So each run closes the store before its loop ends, and the next call dials a
fresh pool. Closing is what `PostgresDisputeStore.close` is for, so nothing
about the store under test is bent to fit.

The row readers go around the store on purpose. They read the tables directly,
on a connection of their own, because what they check — how many rows a race
left behind, whether a mutex row survived — is exactly what the store's own
reads could get wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

import asyncpg

from app.services.dispute_store import DisputeStore

T = TypeVar("T")


def run(store: DisputeStore, coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, closing `store` inside the same loop afterwards."""

    async def once() -> T:
        try:
            return await coro
        finally:
            await store.close()

    return asyncio.run(once())


async def fetch(dsn: str, sql: str, *args: Any) -> list[dict[str, Any]]:
    """Rows as dicts, read on a connection of their own."""
    conn = await asyncpg.connect(dsn)
    try:
        return [dict(row) for row in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


async def execute(dsn: str, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


async def events(dsn: str, dispute_id: str | None = None) -> list[dict[str, Any]]:
    """`dispute_events`, oldest row first: the append-only trail as it stands."""
    if dispute_id is None:
        return await fetch(dsn, "SELECT * FROM dispute_events ORDER BY id")
    return await fetch(dsn, "SELECT * FROM dispute_events WHERE dispute_id = $1 ORDER BY id", dispute_id)


async def statuses(dsn: str, dispute_id: str) -> list[str]:
    return [row["status"] for row in await events(dsn, dispute_id)]


async def claims(dsn: str) -> dict[str, float]:
    """`refund_claims`: dispute id to the moment it was claimed."""
    return {row["dispute_id"]: row["claimed_at"] for row in await fetch(dsn, "SELECT * FROM refund_claims")}


async def settlements(dsn: str) -> list[dict[str, Any]]:
    return await fetch(dsn, "SELECT * FROM workflow_settlements ORDER BY id")
