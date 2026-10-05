"""SnapshotCell: a read model served from memory, rebuilt behind the request.

Pinned here: a fresh snapshot is served as is; a stale one is served AT ONCE
with one rebuild behind it; only a read with nothing it may serve waits, and
never past its bound; a failed build keeps the last snapshot in service and
backs off; `invalidate` retires the held snapshot and fences a build that read
the old state; and the keep-warm schedule rebuilds on age and on a registry
change. Driven directly, without the app.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.services import snapshots
from app.services.snapshots import KeepWarm, SnapshotCell


@dataclass
class _Value:
    n: int
    generated_at: float
    partial: bool = False


class _Builder:
    """Counts builds; each returns the next number, after `delay` seconds."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay
        self.fail: Exception | None = None
        self.partial = False

    async def __call__(self) -> _Value:
        self.calls += 1
        n = self.calls
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return _Value(n=n, generated_at=time.time(), partial=self.partial)


def _cell(builder: _Builder, **kw: object) -> SnapshotCell[_Value]:
    options: dict[str, object] = {
        "fresh_seconds": 60.0,
        "build_timeout_seconds": 5.0,
        "retry_after_failure_seconds": 30.0,
    }
    options.update(kw)
    return SnapshotCell(
        "test",
        lambda: builder(),
        lambda v: json.dumps({"n": v.n}).encode(),
        lambda v: v.generated_at,
        **options,  # type: ignore[arg-type]
    )


def _run(coro_fn: Callable[[], object]) -> object:
    return asyncio.run(coro_fn())  # type: ignore[arg-type]


# ── encoding ────────────────────────────────────────────────────────────────
def test_a_snapshot_carries_its_body_gzip_and_a_weak_etag() -> None:
    builder = _Builder()
    cell = _cell(builder)

    async def go() -> snapshots.Snapshot[_Value] | None:
        return await cell.get(wait_seconds=None)

    snap = _run(go)
    assert isinstance(snap, snapshots.Snapshot)
    assert snap.body == b'{"n": 1}'
    assert gzip.decompress(snap.gzip_body) == snap.body
    assert snap.etag.startswith('W/"') and snap.etag.endswith('"')
    assert snap.etag == snapshots.etag_for(snap.body)
    assert snap.source == "live"


def test_the_etag_changes_with_the_body_and_only_with_it() -> None:
    assert snapshots.etag_for(b"a") == snapshots.etag_for(b"a")
    assert snapshots.etag_for(b"a") != snapshots.etag_for(b"b")


# ── serving ─────────────────────────────────────────────────────────────────
def test_a_fresh_snapshot_is_served_without_a_build() -> None:
    builder = _Builder()
    cell = _cell(builder)

    async def go() -> list[int]:
        first = await cell.get(wait_seconds=None)
        second = await cell.get(wait_seconds=None)
        assert first is second
        return [builder.calls]

    assert _run(go) == [1]


def test_concurrent_first_reads_share_one_build() -> None:
    builder = _Builder(delay=0.05)
    cell = _cell(builder)

    async def go() -> list[object]:
        return list(await asyncio.gather(*(cell.get(wait_seconds=None) for _ in range(8))))

    results = _run(go)
    assert builder.calls == 1
    assert all(r is results[0] for r in results)  # type: ignore[index]


def test_a_stale_snapshot_is_served_at_once_and_rebuilt_behind_the_read() -> None:
    builder = _Builder(delay=0.3)
    cell = _cell(builder, fresh_seconds=0.05)

    async def go() -> tuple[int, int, float, int]:
        first = await cell.get(wait_seconds=None)
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        stale = await cell.get(wait_seconds=None)
        elapsed = time.perf_counter() - started
        assert stale is first
        task = cell._live_task()
        assert task is not None
        await task
        refreshed = await cell.get(wait_seconds=None)
        return first.value.n, refreshed.value.n, elapsed, builder.calls  # type: ignore[union-attr]

    first, refreshed, elapsed, calls = _run(go)  # type: ignore[misc]
    assert (first, refreshed, calls) == (1, 2, 2)
    assert elapsed < 0.05


def test_a_read_with_nothing_to_serve_waits_no_longer_than_its_bound() -> None:
    builder = _Builder(delay=1.0)
    cell = _cell(builder)

    async def go() -> tuple[object, float, bool]:
        started = time.perf_counter()
        snap = await cell.get(wait_seconds=0.05)
        return snap, time.perf_counter() - started, cell.building()

    snap, elapsed, building = _run(go)  # type: ignore[misc]
    assert snap is None
    assert elapsed < 0.5
    assert building is True  # the read gave up; the build it started carries on


def test_a_zero_bound_never_waits_but_starts_the_build() -> None:
    builder = _Builder(delay=0.05)
    cell = _cell(builder)

    async def go() -> tuple[object, bool, object]:
        snap = await cell.get(wait_seconds=0)
        building = cell.building()
        task = cell._live_task()
        assert task is not None
        await task
        return snap, building, await cell.get(wait_seconds=0)

    snap, building, after = _run(go)  # type: ignore[misc]
    assert snap is None and building is True
    assert after is not None and after.value.n == 1  # type: ignore[attr-defined]


def test_a_caller_that_gives_up_does_not_cancel_the_build() -> None:
    builder = _Builder(delay=0.1)
    cell = _cell(builder)

    async def go() -> int:
        reader = asyncio.ensure_future(cell.get(wait_seconds=None))
        await asyncio.sleep(0.02)
        reader.cancel()
        task = cell._live_task()
        assert task is not None
        await task
        snap = cell.current()
        assert snap is not None
        return snap.value.n

    assert _run(go) == 1


def test_a_snapshot_older_than_max_serve_waits_for_a_fresh_one() -> None:
    builder = _Builder()
    cell = _cell(builder, fresh_seconds=0.01, max_serve_seconds=0.05)

    async def go() -> tuple[int, int]:
        first = await cell.get(wait_seconds=None)
        await asyncio.sleep(0.1)
        later = await cell.get(wait_seconds=None)
        return first.value.n, later.value.n  # type: ignore[union-attr]

    assert _run(go) == (1, 2)


def test_must_rebuild_makes_a_read_wait_for_a_replacement() -> None:
    builder = _Builder()
    builder.partial = True
    cell = _cell(builder, must_rebuild=lambda v: v.partial and not builder.partial)

    async def go() -> tuple[int, int, int]:
        partial = await cell.get(wait_seconds=None)
        again = await cell.get(wait_seconds=None)  # still acceptable: served as is
        builder.partial = False
        full = await cell.get(wait_seconds=None)
        return partial.value.n, again.value.n, full.value.n  # type: ignore[union-attr]

    assert _run(go) == (1, 1, 2)


# ── failure ─────────────────────────────────────────────────────────────────
def test_a_failed_build_keeps_the_last_snapshot_and_records_why(caplog: pytest.LogCaptureFixture) -> None:
    builder = _Builder()
    cell = _cell(builder, fresh_seconds=0.01)

    async def go() -> tuple[int, int]:
        first = await cell.get(wait_seconds=None)
        builder.fail = RuntimeError("rpc down")
        await asyncio.sleep(0.02)
        served = await cell.get(wait_seconds=None)
        task = cell._live_task()
        assert task is not None
        await task
        again = await cell.get(wait_seconds=None)
        assert served is first and again is first
        return builder.calls, again.value.n  # type: ignore[union-attr]

    with caplog.at_level("WARNING"):
        calls, n = _run(go)  # type: ignore[misc]
    assert (calls, n) == (2, 1)
    status = cell.status()
    assert status.ready and status.last_error == "RuntimeError: rpc down"
    assert status.failing_since is not None
    assert "snapshot test build failed" in caplog.text
    assert "still serving" in caplog.text


def test_a_failing_cell_backs_off_instead_of_building_per_request() -> None:
    builder = _Builder()
    builder.fail = RuntimeError("down")
    cell = _cell(builder, retry_after_failure_seconds=60.0)

    async def go() -> list[object]:
        results = [await cell.get(wait_seconds=None) for _ in range(5)]
        assert cell.refresh() is None  # still backing off
        assert cell.refresh(force=True) is not None  # unless forced
        return results

    results = _run(go)
    assert results == [None] * 5
    assert builder.calls == 2


def test_a_build_past_its_timeout_fails_and_keeps_the_last_snapshot() -> None:
    builder = _Builder()
    cell = _cell(builder, fresh_seconds=0.01, build_timeout_seconds=0.05)

    async def go() -> tuple[int, str | None]:
        await cell.get(wait_seconds=None)
        builder.delay = 1.0
        await asyncio.sleep(0.02)
        cell.invalidate()
        served = await cell.get(wait_seconds=None)  # waits for the build: it times out
        return served.value.n, cell.status().last_error  # type: ignore[union-attr]

    assert _run(go) == (1, "build timed out")


def test_a_recovery_clears_the_failure(caplog: pytest.LogCaptureFixture) -> None:
    builder = _Builder()
    builder.fail = RuntimeError("down")
    cell = _cell(builder, retry_after_failure_seconds=0.0)

    async def go() -> object:
        await cell.get(wait_seconds=None)
        builder.fail = None
        return await cell.get(wait_seconds=None)

    with caplog.at_level("INFO"):
        snap = _run(go)
    assert snap is not None
    assert cell.status().last_error is None and cell.status().failing_since is None
    assert "snapshot test recovered" in caplog.text


# ── invalidation ────────────────────────────────────────────────────────────
def test_invalidate_makes_the_next_read_wait_for_a_build_started_after_it() -> None:
    builder = _Builder()
    cell = _cell(builder)

    async def go() -> tuple[int, int]:
        first = await cell.get(wait_seconds=None)
        cell.invalidate()
        after = await cell.get(wait_seconds=None)
        return first.value.n, after.value.n  # type: ignore[union-attr]

    assert _run(go) == (1, 2)


def test_an_invalidated_snapshot_is_still_served_when_the_wait_runs_out() -> None:
    builder = _Builder()
    cell = _cell(builder)

    async def go() -> tuple[object, object]:
        first = await cell.get(wait_seconds=None)
        builder.delay = 0.5
        cell.invalidate()
        return first, await cell.get(wait_seconds=0.01)

    first, served = _run(go)  # type: ignore[misc]
    assert served is first


def test_a_build_running_across_an_invalidate_goes_round_again() -> None:
    """The build in flight read the state from BEFORE the change, so the cell
    rebuilds once more instead of settling on it."""
    builder = _Builder(delay=0.05)
    cell = _cell(builder)

    async def go() -> tuple[int, int]:
        task = cell.refresh()
        assert task is not None
        await asyncio.sleep(0.01)
        cell.invalidate()
        await task
        snap = cell.current()
        assert snap is not None
        return snap.value.n, builder.calls

    assert _run(go) == (2, 2)


# ── seeding ─────────────────────────────────────────────────────────────────
def test_a_seed_is_served_until_a_live_build_replaces_it_and_never_after() -> None:
    builder = _Builder()
    cell = _cell(builder)
    old = snapshots.encode(
        _Value(0, time.time() - 600), b'{"n": 0}', time.time() - 600, source="persisted", age_seconds=600
    )

    async def go() -> tuple[object, object, bool]:
        assert cell.seed(old) is True
        served = await cell.get(wait_seconds=0)  # stale: served, with a build behind it
        task = cell._live_task()
        assert task is not None
        await task
        return served, cell.current(), cell.seed(old)

    served, live, reseeded = _run(go)  # type: ignore[misc]
    assert served is old
    assert live.source == "live" and live.value.n == 1  # type: ignore[attr-defined]
    assert reseeded is False


def test_stored_listeners_see_every_live_snapshot() -> None:
    builder = _Builder()
    cell = _cell(builder)
    seen: list[int] = []
    cell.on_stored(lambda snap: seen.append(snap.value.n))
    cell.on_stored(lambda snap: (_ for _ in ()).throw(RuntimeError("listener bug")))

    async def go() -> None:
        await cell.get(wait_seconds=None)
        cell.invalidate()
        await cell.get(wait_seconds=None)

    _run(go)
    assert seen == [1, 2]


# ── keep-warm ───────────────────────────────────────────────────────────────
def test_keep_warm_builds_an_empty_cell_then_rebuilds_on_age() -> None:
    builder = _Builder()
    cell = _cell(builder)
    schedule = KeepWarm(cell=cell, every_seconds=0.05)

    async def go() -> list[int]:
        calls = []
        schedule.tick()
        await asyncio.sleep(0.01)
        calls.append(builder.calls)
        schedule.tick()  # fresh: nothing to do
        await asyncio.sleep(0.01)
        calls.append(builder.calls)
        await asyncio.sleep(0.06)
        schedule.tick()
        await asyncio.sleep(0.01)
        calls.append(builder.calls)
        return calls

    assert _run(go) == [1, 1, 2]


def test_keep_warm_rebuilds_on_a_fingerprint_change_but_not_too_often() -> None:
    builder = _Builder()
    cell = _cell(builder)
    print_ = {"v": 1}
    schedule = KeepWarm(cell=cell, every_seconds=60.0, fingerprint=lambda: print_["v"], min_change_rebuild_seconds=0.05)

    async def go() -> list[int]:
        calls = []
        schedule.tick()
        await asyncio.sleep(0.01)
        print_["v"] = 2
        schedule.tick()  # changed, but the snapshot is younger than the minimum
        await asyncio.sleep(0.01)
        calls.append(builder.calls)
        await asyncio.sleep(0.05)
        schedule.tick()
        await asyncio.sleep(0.01)
        calls.append(builder.calls)
        await asyncio.sleep(0.06)
        schedule.tick()  # unchanged since that build: nothing
        await asyncio.sleep(0.01)
        calls.append(builder.calls)
        return calls

    assert _run(go) == [1, 2, 2]


def test_keep_warm_waits_while_not_ready() -> None:
    builder = _Builder()
    cell = _cell(builder)
    ready = {"v": False}
    schedule = KeepWarm(cell=cell, every_seconds=0.0, ready=lambda: ready["v"])

    async def go() -> list[int]:
        schedule.tick()
        await asyncio.sleep(0.01)
        first = builder.calls
        ready["v"] = True
        schedule.tick()
        await asyncio.sleep(0.01)
        return [first, builder.calls]

    assert _run(go) == [0, 1]


def test_start_runs_the_refresher_and_boot_hooks_and_stop_ends_them(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = _Builder()
    cell = _cell(builder)
    booted: list[str] = []

    async def hook() -> None:
        booted.append("hook")

    async def broken_hook() -> None:
        raise RuntimeError("hook bug")

    monkeypatch.setattr(snapshots, "_schedules", [KeepWarm(cell=cell, every_seconds=60.0)])
    monkeypatch.setattr(snapshots, "_boot_hooks", [hook, broken_hook])
    monkeypatch.setattr(snapshots, "KEEP_WARM_ENABLED", True)
    monkeypatch.setattr(snapshots, "TICK_SECONDS", 0.01)

    async def go() -> None:
        snapshots.start()
        snapshots.start()  # idempotent
        await asyncio.sleep(0.05)
        await snapshots.stop()

    _run(go)
    assert booted == ["hook"]
    assert builder.calls == 1
    assert snapshots._loop_task is None


def test_start_does_nothing_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = _Builder()
    monkeypatch.setattr(snapshots, "_schedules", [KeepWarm(cell=_cell(builder), every_seconds=0.0)])
    monkeypatch.setattr(snapshots, "KEEP_WARM_ENABLED", False)

    async def go() -> None:
        snapshots.start()
        await asyncio.sleep(0.02)
        await snapshots.stop()

    _run(go)
    assert builder.calls == 0
