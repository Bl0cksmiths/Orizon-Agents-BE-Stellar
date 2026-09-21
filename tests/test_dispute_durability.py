"""Story 4.02's hardest acceptance criterion — "the dispute survives a restart".

tests/test_dispute_store.py asserts the SQL a fake pool receives, which proves
the statements are right and proves nothing at all about a restart. This file
models the restart itself, the way the failure actually happens: Render's free
instance spins down after ~15 minutes idle and comes back FROM THE IMAGE, so a
dispute window measured in hours outlives several of this service's processes.

The boundary is `process()` below. The DATABASE survives it — that is what
DATABASE_URL buys — and everything the process held does not: the store object,
its connection pool, and the resolver's singleton. The database here is the same
FakePool the store tests use, kept across the boundary on purpose, because what
it holds is ROWS. A store that stopped writing rows — a cache in front of the
tables, a settlement kept only in the object, a transition that updated instead
of appending — would show up here as an empty second process, which is exactly
the bug this criterion exists to catch.

Hermetic: no database and no network. `database_url` is set per test and
`_create_pool` is replaced, so nothing dials anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from test_dispute_store import JOB, FakePool, a_dispute, a_settlement

from app.services import dispute_store
from app.services.dispute_store import DisputeStore

DSN = "postgres://user:pw@db.example.invalid/orizon"


@pytest.fixture(autouse=True)
def reset_singleton() -> Iterator[None]:
    """The resolver is a module-level singleton; no test may inherit another's."""
    dispute_store._store = None
    yield
    dispute_store._store = None


@contextmanager
def process(monkeypatch: pytest.MonkeyPatch, database: FakePool) -> Iterator[DisputeStore]:
    """One backend process, start to stop.

    Entering resolves the store the way the app does — from `database_url`,
    through the real `get_dispute_store()`, over `database`. Leaving runs the
    real shutdown and then throws away everything this process held, which is
    what a restart does to a container that comes back from the image.
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


def test_a_dispute_opened_before_a_restart_is_still_there_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance criterion, in one test: a buyer disputes a step, the
    service restarts, and the dispute is still open with the reason they gave."""
    database = FakePool()

    with process(monkeypatch, database) as store:
        asyncio.run(store.record_settlement(a_settlement()))
        opened = asyncio.run(store.open_dispute(a_dispute()))
        first_store = store

    # The process is gone: the store object was closed and released, and the
    # singleton with it. Nothing it learned at runtime is left.
    assert dispute_store._store is None
    assert database.closed == 1

    with process(monkeypatch, database) as store:
        assert store is not first_store
        restored = asyncio.run(store.get_dispute(opened.id))
        assert restored is not None
        assert restored == opened
        assert restored.status == "open"
        assert restored.reason == "the summary was empty"
        assert restored.creditable_usdc == 1.5
        # And found by the step as well, which is the lookup the second
        # "dispute this step" request makes.
        assert asyncio.run(store.find_dispute(JOB, 0)) == opened
