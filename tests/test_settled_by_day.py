"""`count_settled_by_day` — the settled-workflow counts the overview reports.

Run against both stores, because the overview reads whichever one is
configured: the in-memory fallback directly, and Postgres through the real SQL
(`pg_dsn`, tests/conftest.py), since only the database can say what
`DISTINCT ON` and `floor(settled_at / 86400)` actually do.

What is pinned, on both:
  - one workflow is one job: a job that wrote a second row (the seal) counts once;
  - the newest row for a job decides its day, as every other read here does;
  - days are UTC calendar days, split exactly at midnight;
  - an empty store answers an empty map, not an error.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from pg_support import run
from test_dispute_store import a_settlement

from app.services.dispute_store import (
    SECONDS_PER_DAY,
    DisputeStore,
    InMemoryDisputeStore,
    PostgresDisputeStore,
)

DAY = 20_000  # 2024-10-04, an arbitrary UTC day
MIDNIGHT = DAY * SECONDS_PER_DAY


def _job(n: int) -> str:
    return f"{n:064x}"


async def _record_history(store: DisputeStore) -> dict[int, int]:
    # Two jobs on DAY, one of them written twice (charge, then seal).
    await store.record_settlement(a_settlement(job_id_hex=_job(1), settled_at=MIDNIGHT + 10, proof_tx=None))
    await store.record_settlement(a_settlement(job_id_hex=_job(1), settled_at=MIDNIGHT + 10, proof_tx="tx_seal"))
    await store.record_settlement(a_settlement(job_id_hex=_job(2), settled_at=MIDNIGHT))
    # Half a second before midnight is still the previous UTC day.
    await store.record_settlement(a_settlement(job_id_hex=_job(3), settled_at=MIDNIGHT - 0.5))
    # Three days later.
    await store.record_settlement(a_settlement(job_id_hex=_job(4), settled_at=MIDNIGHT + 3 * SECONDS_PER_DAY + 1))
    # A job whose newest row moved it to a later day: the newest row wins.
    await store.record_settlement(a_settlement(job_id_hex=_job(5), settled_at=MIDNIGHT - SECONDS_PER_DAY))
    await store.record_settlement(a_settlement(job_id_hex=_job(5), settled_at=MIDNIGHT + 3 * SECONDS_PER_DAY))
    return await store.count_settled_by_day()


EXPECTED = {DAY - 1: 1, DAY: 2, DAY + 3: 2}


def test_in_memory_counts_jobs_per_utc_day() -> None:
    assert asyncio.run(_record_history(InMemoryDisputeStore())) == EXPECTED


def test_in_memory_empty_store_has_no_days() -> None:
    assert asyncio.run(InMemoryDisputeStore().count_settled_by_day()) == {}


def test_postgres_counts_jobs_per_utc_day(pg_dsn: str) -> None:
    store = PostgresDisputeStore(pg_dsn)
    assert run(store, _record_history(store)) == EXPECTED


def test_postgres_empty_store_has_no_days(pg_dsn: str) -> None:
    store = PostgresDisputeStore(pg_dsn)
    assert run(store, store.count_settled_by_day()) == {}


class _Pool:
    """Answers `fetch` with the rows given, recording what was sent."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.sent: list[str] = []

    async def execute(self, sql: str, *args: object, timeout: float | None = None) -> None:
        return None

    @contextlib.asynccontextmanager
    async def acquire(self, *, timeout: float | None = None) -> AsyncIterator[_Pool]:
        """The schema's DDL connection (app/services/pg_schema.py): this fake serves as one."""
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield

    async def fetch(self, sql: str, *args: object, timeout: float | None = None) -> list[dict[str, object]]:
        self.sent.append(sql)
        return self.rows


@pytest.mark.parametrize("day, settled", [(DAY, 3), (DAY + 1, 0)])
def test_postgres_coerces_driver_numbers_to_int(day: int, settled: int) -> None:
    """A bigint/count can come back as a Decimal through a proxy; the map is ints."""
    from decimal import Decimal

    pool = _Pool([{"day": Decimal(day), "settled": Decimal(settled)}])
    store = PostgresDisputeStore("postgres://unused", pool=pool)
    result = asyncio.run(store.count_settled_by_day())
    assert result == {day: settled}
    assert all(type(k) is int and type(v) is int for k, v in result.items())
    assert "DISTINCT ON (job_id_hex)" in pool.sent[0]
