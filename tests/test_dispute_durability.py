"""Story 4.02's hardest acceptance criterion — "the dispute survives a restart".

This file models the restart itself, the way the failure actually happens:
Render's free instance spins down after ~15 minutes idle and comes back FROM
THE IMAGE, so a dispute window measured in hours outlives several of this
service's processes.

The boundary is `pg_process()` below. The DATABASE survives it — that is what
DATABASE_URL buys — and everything the process held does not: the store object,
its connection pool, and the resolver's singleton. The database is a REAL
Postgres (conftest `pg_dsn`), kept across the boundary on purpose, because what
it holds is ROWS: the second process dials its own pool, re-runs the DDL over
tables that already exist, and reads back what the first one committed. A store
that stopped writing rows — a cache in front of the tables, a settlement kept
only in the object, a transition that updated instead of appending — would show
up here as an empty second process, which is exactly the bug this criterion
exists to catch.

These tests used to restart over the same Python FakePool object, which proved
the store re-reads through the seam and nothing about DDL, types or
persistence. `process()`, the FakePool version, stays only because
tests/test_dispute_receipt_fields.py still uses it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pg_support import run, statuses
from test_dispute_store import JOB, STEPS, TASK, FakePool, a_dispute, a_settlement

from app.services import dispute_store
from app.services.dispute_store import (
    DisputeRecord,
    DisputeStore,
    DuplicateDisputeError,
    InMemoryDisputeStore,
    PostgresDisputeStore,
    SettlementRecord,
)

DSN = "postgres://user:pw@db.example.invalid/orizon"


@pytest.fixture(autouse=True)
def reset_singleton() -> Iterator[None]:
    """The resolver is a module-level singleton; no test may inherit another's."""
    dispute_store._store = None
    yield
    dispute_store._store = None


@contextmanager
def pg_process(monkeypatch: pytest.MonkeyPatch, dsn: str) -> Iterator[DisputeStore]:
    """One backend process, start to stop, over a real database.

    Entering resolves the store the way the app does — from `database_url`,
    through the real `get_dispute_store()`. Every call made inside goes through
    `pg_support.run`, which closes the pool inside the loop that dialled it;
    leaving runs the real shutdown and throws the singleton away, which is what
    a restart does to a container that comes back from the image.
    """
    monkeypatch.setattr(dispute_store.settings, "database_url", dsn)
    dispute_store._store = None
    try:
        store = dispute_store.get_dispute_store()
        assert isinstance(store, PostgresDisputeStore)
        yield store
    finally:
        asyncio.run(dispute_store.close_dispute_store())


@contextmanager
def process(monkeypatch: pytest.MonkeyPatch, database: FakePool) -> Iterator[DisputeStore]:
    """One backend process over a FakePool, for tests/test_dispute_receipt_fields.py.

    A restart over the same Python object says nothing about persistence, so
    the restart claims in this file are made over `pg_process` instead.

    Entering resolves the store the way the app does — from `database_url`,
    through the real `get_dispute_store()`, over `database`. Leaving runs the
    real shutdown and then throws away everything this process held.
    """

    async def _create_pool(self: Any) -> FakePool:
        return database

    monkeypatch.setattr(dispute_store.settings, "database_url", DSN)
    monkeypatch.setattr(dispute_store.PostgresDisputeStore, "_create_pool", _create_pool)
    dispute_store._store = None
    try:
        yield dispute_store.get_dispute_store()
    finally:
        asyncio.run(dispute_store.close_dispute_store())


def test_a_dispute_opened_before_a_restart_is_still_there_after_it(
    monkeypatch: pytest.MonkeyPatch, pg_dsn: str
) -> None:
    """The acceptance criterion, in one test: a buyer disputes a step, the
    service restarts, and the dispute is still open with the reason they gave."""
    with pg_process(monkeypatch, pg_dsn) as store:

        async def before() -> DisputeRecord:
            await store.record_settlement(a_settlement())
            return await store.open_dispute(a_dispute())

        opened = run(store, before())
        first_store = store

    # The process is gone: the store object was closed and released, and the
    # singleton with it. Nothing it learned at runtime is left.
    assert dispute_store._store is None

    with pg_process(monkeypatch, pg_dsn) as store:
        assert store is not first_store

        async def after() -> tuple[DisputeRecord | None, DisputeRecord | None]:
            return await store.get_dispute(opened.id), await store.find_dispute(JOB, 0)

        restored, by_step = run(store, after())
        assert restored is not None
        assert restored == opened
        assert restored.status == "open"
        assert restored.reason == "the summary was empty"
        assert restored.creditable_usdc == 1.5
        # And found by the step as well, which is the lookup the second
        # "dispute this step" request makes.
        assert by_step == opened


def test_the_window_a_buyer_was_promised_is_the_window_after_the_restart(
    monkeypatch: pytest.MonkeyPatch, pg_dsn: str
) -> None:
    """The settlement is what the window is measured from, and the step prices
    are what a credit is computed from. Losing either turns a live dispute into
    one nothing can be decided about — the state app/state.py would leave it in,
    since it evicts FINISHED tasks first."""
    with pg_process(monkeypatch, pg_dsn) as store:
        run(store, store.record_settlement(a_settlement()))

    with pg_process(monkeypatch, pg_dsn) as store:

        async def after() -> tuple[SettlementRecord | None, SettlementRecord | None]:
            return await store.get_settlement(JOB), await store.get_settlement_by_task(TASK)

        settled, by_task = run(store, after())
        assert settled is not None
        # Not recomputed from DISPUTE_WINDOW_SECONDS at read time: the buyer was
        # told a closing time, and a restart must not move it either.
        assert settled.window_closes_at == 1_700_086_400.0
        assert settled.settled_usdc == 3.75
        assert settled.steps == STEPS
        assert settled.charge_tx == "tx_charge"
        # And reachable from the task id too, which is all the task view has.
        assert by_task == settled


def test_a_resolved_dispute_does_not_reopen_after_a_restart(monkeypatch: pytest.MonkeyPatch, pg_dsn: str) -> None:
    """The dangerous direction. A credited dispute that came back as `open`
    would be paid a second time by the next run of story 4.03, and the refund
    transaction proving the first payment would be gone."""
    with pg_process(monkeypatch, pg_dsn) as store:

        async def before() -> DisputeRecord:
            await store.record_settlement(a_settlement())
            opened = await store.open_dispute(a_dispute())
            await store.append_status(opened.id, "credited", refund_tx="tx_refund")
            return opened

        opened = run(store, before())

    with pg_process(monkeypatch, pg_dsn) as store:
        restored = run(store, store.get_dispute(opened.id))
        assert restored is not None
        assert restored.status == "credited"
        assert restored.refund_tx == "tx_refund"
        assert restored.resolved_at is not None
        # The opening row survived the restart as well: the audit trail is what
        # a chargeback is answered with, so a transition must not have replaced
        # what the dispute said when it was opened.
        assert asyncio.run(statuses(pg_dsn, opened.id)) == ["open", "credited"]
        assert restored.reason == "the summary was empty"


def test_the_duplicate_rule_still_holds_after_a_restart(monkeypatch: pytest.MonkeyPatch, pg_dsn: str) -> None:
    """The rule lives in the index, so it is as durable as the rows are. A
    buyer who disputes a step, waits out a spin-down and disputes it again gets
    the dispute they already opened — not a second one, and not a second
    credit."""
    with pg_process(monkeypatch, pg_dsn) as store:

        async def before() -> DisputeRecord:
            await store.record_settlement(a_settlement())
            return await store.open_dispute(a_dispute())

        first = run(store, before())

    with pg_process(monkeypatch, pg_dsn) as store:
        with pytest.raises(DuplicateDisputeError) as excinfo:
            run(store, store.open_dispute(a_dispute(id="dsp_after_restart", reason="trying again")))

        assert excinfo.value.existing == first
        assert run(store, store.get_dispute("dsp_after_restart")) is None
        assert asyncio.run(statuses(pg_dsn, first.id)) == ["open"]


def test_the_in_memory_default_loses_the_dispute_at_the_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a bug — the whole reason PostgresDisputeStore exists, stated as a
    test so the gap is visible rather than assumed. Without DATABASE_URL the
    criterion above is false, which is what the startup log warns about."""
    monkeypatch.setattr(dispute_store.settings, "database_url", "")

    first = dispute_store.get_dispute_store()
    assert isinstance(first, InMemoryDisputeStore)
    asyncio.run(first.record_settlement(a_settlement()))
    opened = asyncio.run(first.open_dispute(a_dispute()))

    asyncio.run(dispute_store.close_dispute_store())
    second = dispute_store.get_dispute_store()

    assert second is not first
    assert asyncio.run(second.get_dispute(opened.id)) is None
    assert asyncio.run(second.get_settlement(JOB)) is None
