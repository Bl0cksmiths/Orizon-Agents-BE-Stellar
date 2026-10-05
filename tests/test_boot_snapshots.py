"""Lifespan runs the snapshot refresher and stops it, and boot never waits on it."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import snapshot_store, snapshots
from app.services.snapshots import KeepWarm, SnapshotCell


def test_lifespan_starts_the_refresher_without_waiting_on_it_and_stops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[float] = []

    async def slow_build() -> dict[str, float]:
        await asyncio.sleep(0.3)
        built.append(time.time())
        return {"at": time.time()}

    cell: SnapshotCell[dict[str, float]] = SnapshotCell(
        "boot-test",
        slow_build,
        lambda v: json.dumps(v).encode(),
        lambda v: v["at"],
        fresh_seconds=60.0,
        build_timeout_seconds=5.0,
        retry_after_failure_seconds=5.0,
    )
    closed: list[str] = []
    real_close = snapshot_store.close_snapshot_store

    async def close() -> None:
        closed.append("store")
        await real_close()

    monkeypatch.setattr(snapshots, "KEEP_WARM_ENABLED", True)
    monkeypatch.setattr(snapshots, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(snapshots, "_schedules", [KeepWarm(cell=cell, every_seconds=60.0)])
    monkeypatch.setattr(snapshots, "_boot_hooks", [])
    monkeypatch.setattr(main, "close_snapshot_store", close)

    started = time.monotonic()
    with TestClient(main.app) as client:
        booted = time.monotonic() - started
        assert cell.building() or not built  # the build runs behind boot
        client.portal.call(asyncio.sleep, 0.5)
        assert len(built) == 1
        assert snapshots._loop_task is not None
    assert booted < 0.3
    assert snapshots._loop_task is None
    assert closed == ["store"]
