"""S13: the registry sync's first pass, raced against the planner at boot.

The loop's first pass ran in the background while the reputation pre-warm read
`state.list_agents()` — the seeded catalog alone on a fresh process — so after
every restart the first plans were built without any on-chain agent, and each
one's first reputation read was cold: the prior, failing open. Boot now waits,
bounded, for that first pass before the pre-warm, and pre-warms what it found.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from app.services import registry_sync

LOGGER_NAME = "app.services.registry_sync"


@pytest.fixture(autouse=True)
def clean_loop():
    yield
    registry_sync._task = None
    registry_sync._first_pass = None


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


def test_the_wait_returns_once_the_first_pass_finishes(monkeypatch):
    async def scenario() -> tuple[bool, bool]:
        gate = asyncio.Event()

        async def sync_once() -> int:
            await gate.wait()
            return 1

        monkeypatch.setattr(registry_sync, "sync_once", sync_once)
        registry_sync.start()
        waiting = asyncio.ensure_future(registry_sync.wait_first_pass(5.0))
        await asyncio.sleep(0.05)
        early = waiting.done()
        gate.set()
        finished = await waiting
        await registry_sync.stop()
        return early, finished

    early, finished = asyncio.run(scenario())

    assert early is False  # it really waited for the pass
    assert finished is True


def test_a_hung_first_pass_times_out_and_the_loop_carries_on(monkeypatch, caplog):
    """A hung RPC holds a worker thread, not boot. The wait gives up at its
    bound with one WARNING; the pass is NOT cancelled, and lands afterwards."""
    rpc = threading.Event()
    landed: list[int] = []

    async def sync_once() -> int:
        await asyncio.to_thread(rpc.wait, 10)
        landed.append(1)
        return 1

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)

    async def scenario() -> tuple[bool, float, bool]:
        registry_sync.start()
        started = time.monotonic()
        finished = await registry_sync.wait_first_pass(0.2)
        elapsed = time.monotonic() - started
        loop_alive = registry_sync._task is not None and not registry_sync._task.done()
        rpc.set()
        first = registry_sync._first_pass
        assert first is not None
        await asyncio.wait_for(first.wait(), timeout=5.0)
        await registry_sync.stop()
        return finished, elapsed, loop_alive

    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            finished, elapsed, loop_alive = asyncio.run(scenario())
    finally:
        rpc.set()

    assert finished is False
    assert elapsed < 1.0
    assert loop_alive is True
    assert landed == [1]  # the pass boot stopped waiting for still finished
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "did not finish within 0.2 s" in warnings[0]


def test_a_failed_first_pass_still_releases_boot(monkeypatch):
    async def sync_once() -> int:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)

    async def scenario() -> bool:
        registry_sync.start()
        finished = await registry_sync.wait_first_pass(5.0)
        await registry_sync.stop()
        return finished

    assert asyncio.run(scenario()) is True


def test_a_zero_bound_does_not_wait_and_does_not_warn(monkeypatch, caplog):
    async def sync_once() -> int:
        await asyncio.sleep(10)
        return 0

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)

    async def scenario() -> bool:
        registry_sync.start()
        finished = await registry_sync.wait_first_pass(0.0)
        await registry_sync.stop()
        return finished

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert asyncio.run(scenario()) is False
    assert _warnings(caplog) == []


def test_without_a_loop_there_is_nothing_to_wait_for():
    assert asyncio.run(registry_sync.wait_first_pass(5.0)) is True
