"""Lifespan and the durable task store: flushed at shutdown, warmed at boot.

Shutdown: the task journal is closed AFTER in-flight runs drain, so a run's
final state reaches the store even when it lands during the drain.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_task_persistence import FakeStore, _task
from test_task_persistence import store as store  # noqa: F401 — the shared fixture

import app.main as main
from app.services import execution_svc, task_store, task_warmup
from app.services.task_store import PostgresTaskStore, TaskRow, WriteBatch
from app.state import state


@pytest.fixture
def no_background_runs() -> Iterator[None]:
    saved = set(execution_svc._background_tasks)
    yield
    execution_svc._background_tasks.clear()
    execution_svc._background_tasks.update(saved)


def test_shutdown_flushes_a_run_that_finishes_during_the_drain(store: FakeStore, no_background_runs: None) -> None:
    task = _task(1, status="running")

    async def run_ending_at_shutdown() -> None:
        await asyncio.sleep(0.2)
        state.put_task(task.model_copy(update={"status": "complete"}))

    with TestClient(main.app) as client:

        async def start() -> None:
            state.add_task(task)
            execution_svc._background_tasks.add(asyncio.get_running_loop().create_task(run_ending_at_shutdown()))

        client.portal.call(start)

    assert store.tasks[task.id].status == "complete"


# ── the boot warm-up ────────────────────────────────────────────────────────
class _ListingStore(FakeStore):
    """FakeStore plus the newest-first listing the warm-up asks for."""

    def __init__(self, base: FakeStore) -> None:
        self.__dict__.update(base.__dict__)
        self.list_delay = 0.0
        self.list_down = False

    async def recent_task_ids(self, limit: int) -> list[str]:
        if self.list_delay:
            await asyncio.sleep(self.list_delay)
        if self.list_down:
            raise OSError("could not connect to server")
        rows = sorted(self.tasks.values(), key=lambda r: r.started_at, reverse=True)
        return [r.task_id for r in rows[:limit]]


@pytest.fixture
def listing(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> _ListingStore:
    fake = _ListingStore(store)
    monkeypatch.setattr(task_store, "get_task_store", lambda: fake)
    return fake


def _seed(listing: _ListingStore, n: int) -> list[str]:
    """`n` tasks a previous process stored, and a fresh process's empty cache
    (the `store` fixture restores whatever the suite held before)."""
    state.tasks.clear()
    state.task_order.clear()
    ids = []
    for i in range(n):
        task = _task(100 + i).model_copy(update={"started_at": time.time() - 1000 + i})
        listing.seed(task)
        ids.append(task.id)
    return ids


def test_the_warm_up_loads_the_newest_tasks_newest_first(listing: _ListingStore) -> None:
    ids = _seed(listing, 5)
    for task_id in ids:
        state.tasks.pop(task_id, None)

    assert asyncio.run(task_warmup.warm_recent_tasks(limit=3)) == 3
    assert [t.id for t in state.recent_tasks(limit=3)] == list(reversed(ids))[:3]
    assert ids[0] not in state.tasks  # older than the limit: left to read-through


def test_the_warm_up_is_bounded_and_never_raises(listing: _ListingStore, caplog: pytest.LogCaptureFixture) -> None:
    _seed(listing, 3)
    listing.list_delay = 5.0
    with caplog.at_level("WARNING"):
        assert asyncio.run(task_warmup.warm_recent_tasks(budget_seconds=0.05)) == 0
    assert "stopped at its 0 s budget" in caplog.text

    listing.list_delay = 0.0
    listing.list_down = True
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert asyncio.run(task_warmup.warm_recent_tasks()) == 0
    assert "the store could not be read (OSError" in caplog.text


def test_the_warm_up_is_a_no_op_without_a_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_store, "get_task_store", lambda: None)
    assert asyncio.run(task_warmup.warm_recent_tasks()) == 0


def test_after_a_restart_the_task_list_is_not_empty(listing: _ListingStore) -> None:
    ids = _seed(listing, 4)
    for task_id in ids:
        state.tasks.pop(task_id, None)
        if task_id in state.task_order:
            state.task_order.remove(task_id)

    with TestClient(main.app) as client:
        joined = [t for t in main._boot_tasks if t.get_name() == "boot-task-warmup"]

        async def join() -> None:
            if joined:
                await asyncio.wait(joined)

        client.portal.call(join)
        listed = {t["id"] for t in client.get("/api/tasks").json()}
    assert set(ids) <= listed


def test_a_slow_store_never_slows_boot(listing: _ListingStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured, with and without the warm-up: boot is the same either way."""
    _seed(listing, 3)
    listing.list_delay = 2.0

    def boot() -> float:
        started = time.monotonic()
        with TestClient(main.app):
            booted = time.monotonic() - started
        return booted

    with_warmup = boot()

    async def nothing() -> None:
        return None

    monkeypatch.setattr(main, "_warm_tasks", nothing)
    without = boot()
    assert with_warmup < 0.5, f"boot took {with_warmup:.2f} s with a 2 s store behind the warm-up"
    assert abs(with_warmup - without) < 0.25


def test_the_postgres_listing_is_newest_first_and_bounded(pg_dsn: str) -> None:
    async def go() -> list[str]:
        db = PostgresTaskStore(pg_dsn)
        try:
            rows = [
                TaskRow.of(
                    _task(200 + i).model_copy(update={"started_at": 1_000.0 + i}),
                    read_token_sha256=None,
                    boot_id="b" * 16,
                    updated_at=1_000.0 + i,
                )
                for i in range(4)
            ]
            await db.write(WriteBatch(tasks=tuple(rows)))
            return await task_warmup._recent_ids(db, 3)
        finally:
            await db.close()

    assert asyncio.run(go()) == [_task(203).id, _task(202).id, _task(201).id]
