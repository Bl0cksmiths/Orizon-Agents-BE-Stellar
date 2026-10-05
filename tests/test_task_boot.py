"""Lifespan and the durable task store: flushed at shutdown, warmed at boot.

Shutdown: the task journal is closed AFTER in-flight runs drain, so a run's
final state reaches the store even when it lands during the drain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_task_persistence import FakeStore, _task
from test_task_persistence import store as store  # noqa: F401 — the shared fixture

import app.main as main
from app.services import execution_svc
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
