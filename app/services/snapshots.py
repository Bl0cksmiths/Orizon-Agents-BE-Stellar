"""
Read snapshots — public read models built behind the request, served from memory.

The dashboard's reads are aggregates over the whole registry: the overview reads
every agent's reputation, the reputation batch is one ledger read per agent, and
the adoption report (D-091) runs a settlement scan per external agent. Computed
on the request that asks, they cost what the chain costs — 2.7 s, 2.9 s and
several MINUTES measured live — and a reviewer opening the page pays it.

A `SnapshotCell` turns each into a value that is always ready:

  - the last good build is held in memory, already encoded (JSON, its gzip,
    and an ETag), so serving it is a dictionary read and a socket write;
  - a read past `fresh_seconds` is still answered at once, and starts ONE
    rebuild behind itself (stale-while-revalidate, single-flight);
  - a read waits only when there is nothing it may serve: no snapshot yet, one
    `invalidate` has retired (upstream state changed under it), one the cell's
    own `must_rebuild` refuses, or one older than `max_serve_seconds`. Even then
    it waits at most the caller's bound, and a caller that walks away never
    cancels the build (it is shielded) — the result lands for the next read;
  - a build that fails or overruns `build_timeout_seconds` keeps the previous
    snapshot in service, is logged on a duty cycle, and is not retried for
    `retry_after_failure_seconds`, so an outage costs one attempt per window
    rather than one per request.

Freshness is judged on the monotonic clock; `generated_at` (epoch seconds, the
value's own) is what the response states, as `Last-Modified` and the snapshot's
age. `KeepWarm` (below) rebuilds every registered cell on a schedule and when
the registry mirror changes, so on a live process a read essentially never
finds the cell stale.

Single event loop. Every mutation happens on the loop thread; a cell whose
build task belongs to a loop that has since closed (a test's `asyncio.run`)
treats it as gone rather than waiting on a task that will never run.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

SnapshotSource = Literal["live", "persisted"]

# gzip level for the pre-compressed body. 6 is zlib's default: within a few
# percent of 9's ratio on JSON at a third of the CPU, and it is paid once per
# build rather than once per request.
GZIP_LEVEL = 6

# A cell rebuilt again and again because `invalidate` keeps landing mid-build
# stops after this many rounds; the snapshot it stored is still marked retired,
# so the next read starts another.
_MAX_ROUNDS = 3

# A failing cell logs on a duty cycle: the first failure and every recovery at
# once, a continuing outage at most this often.
_FAILURE_LOG_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class Snapshot(Generic[T]):
    """One build of a read model, encoded once for every request that serves it."""

    value: T
    body: bytes  # the JSON exactly as served
    gzip_body: bytes  # `body`, gzip-compressed at build time
    etag: str  # weak validator over `body` (gzip changes the bytes, not the meaning)
    generated_at: float  # epoch seconds: when the value was computed
    source: SnapshotSource  # "persisted": restored from the database at boot
    stored_monotonic: float  # this process's clock, for freshness decisions
    generation: int  # the cell's generation it was built under

    def age_seconds(self, now: float | None = None) -> float:
        """Seconds since the value was computed (wall clock, never negative)."""
        return max(0.0, (time.time() if now is None else now) - self.generated_at)


def etag_for(body: bytes) -> str:
    """A weak ETag over `body`.

    Weak because the same entity goes out gzip-encoded or not depending on the
    request, and a strong validator would have to differ between the two.
    """
    return 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'


def encode(
    value: T,
    body: bytes,
    generated_at: float,
    *,
    source: SnapshotSource = "live",
    generation: int = 0,
    age_seconds: float = 0.0,
) -> Snapshot[T]:
    """Wrap an encoded value as a Snapshot. `age_seconds` back-dates its
    freshness clock (a persisted snapshot is as old as its `generated_at`)."""
    return Snapshot(
        value=value,
        body=body,
        gzip_body=gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0),
        etag=etag_for(body),
        generated_at=generated_at,
        source=source,
        stored_monotonic=time.monotonic() - max(0.0, age_seconds),
        generation=generation,
    )


@dataclass(frozen=True, slots=True)
class CellStatus:
    """What a cell can say about itself, for logs and probes."""

    name: str
    ready: bool  # a snapshot is held
    source: SnapshotSource | None
    age_seconds: float | None
    building: bool
    last_build_ms: int | None
    last_error: str | None  # the latest build's failure; None once one succeeds
    failing_since: float | None  # epoch seconds of the first failure in the current streak


def _describe(e: BaseException) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class SnapshotCell(Generic[T]):
    """The last good build of one read model, and the one rebuild behind it.

    `build` computes the value and `serialize` turns it into the response's
    JSON bytes; `generated_at` reads the value's own timestamp. Both callables
    are looked up on every build, so a module attribute the caller passes as
    `lambda: module.fn()` stays patchable.
    """

    def __init__(
        self,
        name: str,
        build: Callable[[], Awaitable[T]],
        serialize: Callable[[T], bytes],
        generated_at: Callable[[T], float],
        *,
        fresh_seconds: float,
        build_timeout_seconds: float,
        retry_after_failure_seconds: float,
        max_serve_seconds: float | None = None,
        must_rebuild: Callable[[T], bool] | None = None,
        may_build: Callable[[], bool] | None = None,
    ) -> None:
        self.name = name
        self._build = build
        self._serialize = serialize
        self._generated_at = generated_at
        # Plain attributes, so a test (or an operator's patch) can tune them.
        self.fresh_seconds = fresh_seconds
        self.build_timeout_seconds = build_timeout_seconds
        self.retry_after_failure_seconds = retry_after_failure_seconds
        self.max_serve_seconds = max_serve_seconds
        self._must_rebuild = must_rebuild
        # A gate on starting builds at all: the adoption report must not be
        # built from a registry mirror still filling after a restart.
        self._may_build = may_build
        self._listeners: list[Callable[[Snapshot[T]], None]] = []
        self.reset()

    # ── state ──────────────────────────────────────────────────────────────
    def reset(self) -> None:
        """Forget everything: snapshot, failure streak, build (tests)."""
        self._snap: Snapshot[T] | None = None
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._failed_monotonic: float | None = None
        self._failing_since: float | None = None
        self._failure_logged_at = 0.0
        self._last_error: str | None = None
        self._last_build_ms: int | None = None
        # `expire` marks; a build that STARTED after the latest mark clears it.
        self._expired = False
        self._expire_seq = 0

    def current(self) -> Snapshot[T] | None:
        """The snapshot held now, whatever its age. Never starts work."""
        return self._snap

    def on_stored(self, listener: Callable[[Snapshot[T]], None]) -> None:
        """Call `listener` with every live snapshot this cell stores (persistence)."""
        self._listeners.append(listener)

    def age_monotonic(self, snap: Snapshot[T]) -> float:
        return time.monotonic() - snap.stored_monotonic

    def is_fresh(self, snap: Snapshot[T]) -> bool:
        return not self._expired and self.age_monotonic(snap) <= self.fresh_seconds and not self._retired(snap)

    def _retired(self, snap: Snapshot[T]) -> bool:
        """Whether `snap` may not be served without first trying for a newer one."""
        if snap.generation < self._generation:
            return True  # `invalidate` ran after it was built
        if self._must_rebuild is not None and self._must_rebuild(snap.value):
            return True
        return self.max_serve_seconds is not None and self.age_monotonic(snap) > self.max_serve_seconds

    def building(self) -> bool:
        return self._live_task() is not None

    def _live_task(self) -> asyncio.Task[None] | None:
        task = self._task
        if task is None or task.done():
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return task
        # A task left pending on a loop that has since closed never runs again.
        return task if task.get_loop() is loop else None

    def status(self) -> CellStatus:
        snap = self._snap
        return CellStatus(
            name=self.name,
            ready=snap is not None,
            source=snap.source if snap is not None else None,
            age_seconds=round(snap.age_seconds(), 1) if snap is not None else None,
            building=self.building(),
            last_build_ms=self._last_build_ms,
            last_error=self._last_error,
            failing_since=self._failing_since,
        )

    # ── reads ──────────────────────────────────────────────────────────────
    async def get(self, wait_seconds: float | None) -> Snapshot[T] | None:
        """The snapshot to serve now, or None when there is none to serve.

        A fresh snapshot is returned as is. A stale one is returned as well,
        with one rebuild started behind it. Only when there is nothing this
        read may serve — no snapshot, or a retired one — does it wait for a
        build, for at most `wait_seconds` (None: as long as the build takes,
        which `build_timeout_seconds` bounds; 0: not at all). After a wait that
        ran out, the retired snapshot is still returned rather than nothing:
        the newest thing known beats an error, and its age says what it is.
        """
        snap = self._snap
        if snap is not None and not self._retired(snap):
            if self._expired or self.age_monotonic(snap) > self.fresh_seconds:
                self.refresh()
            return snap
        task = self.refresh()
        if task is not None and (wait_seconds is None or wait_seconds > 0):
            # The shield keeps a caller that gives up (or is cancelled) from
            # cancelling the build every other reader is waiting on.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), wait_seconds)
        return self._snap

    # ── writes ─────────────────────────────────────────────────────────────
    def invalidate(self) -> None:
        """Retire the held snapshot: the upstream changed under it.

        It is kept, so a read that cannot wait still has it, but the next read
        waits (bounded) for a build that starts after this call. A build
        already running read the old state, so it goes round once more.
        """
        self._generation += 1

    def expire(self) -> None:
        """Mark the held snapshot stale without retiring it.

        For a change that makes the snapshot out of date but not wrong to show
        for one more poll: the next read is still answered from it at once,
        and starts the rebuild. Nothing is built here, so it is safe to call
        from any path, with or without a running loop.
        """
        self._expired = True
        self._expire_seq += 1

    def seed(self, snap: Snapshot[T]) -> bool:
        """Install a snapshot restored from elsewhere, unless a build has
        already stored one. True when it was installed."""
        if self._snap is not None:
            return False
        self._snap = snap
        return True

    def refresh(self, *, force: bool = False) -> asyncio.Task[None] | None:
        """Start a build unless one is running; return the running build.

        None — nothing started — while the cell is backing off a failed build
        or its `may_build` gate is shut, unless `force`. Requires a running
        event loop.
        """
        running = self._live_task()
        if running is not None:
            return running
        if not force and self._may_build is not None and not self._may_build():
            return None
        if (
            not force
            and self._failed_monotonic is not None
            and time.monotonic() - self._failed_monotonic < self.retry_after_failure_seconds
        ):
            return None
        task = asyncio.get_running_loop().create_task(self._run(), name=f"snapshot:{self.name}")
        self._task = task
        return task

    async def _run(self) -> None:
        """Build until the stored snapshot is current. Never raises: a failure
        is recorded and the previous snapshot stays in service."""
        for _ in range(_MAX_ROUNDS):
            generation = self._generation
            expire_seq = self._expire_seq
            started = time.monotonic()
            try:
                value = await asyncio.wait_for(self._build(), timeout=self.build_timeout_seconds)
                snap = encode(value, self._serialize(value), self._generated_at(value), generation=generation)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._record_failure(e, started)
                return
            self._store(snap, started, expire_seq)
            if self._generation == generation:
                return

    def _store(self, snap: Snapshot[T], started: float, expire_seq: int) -> None:
        current = self._snap
        # A build can only ever move the cell forward: one that started under
        # an older generation than the held snapshot's would put back a value
        # an `invalidate` had already retired.
        if current is not None and current.source == "live" and current.generation > snap.generation:
            return
        self._snap = snap
        if expire_seq == self._expire_seq:
            self._expired = False
        self._last_build_ms = int((time.monotonic() - started) * 1000)
        if self._failed_monotonic is not None:
            logger.info("snapshot %s recovered: rebuilt in %d ms", self.name, self._last_build_ms)
        elif current is None or current.source != "live":
            logger.info("snapshot %s ready: built in %d ms", self.name, self._last_build_ms)
        self._failed_monotonic = None
        self._failing_since = None
        self._last_error = None
        for listener in self._listeners:
            try:
                listener(snap)
            except Exception as e:
                logger.warning("snapshot %s listener failed: %s", self.name, _describe(e))

    def _record_failure(self, e: Exception, started: float) -> None:
        now = time.monotonic()
        first = self._failed_monotonic is None
        self._failed_monotonic = now
        self._last_error = "build timed out" if isinstance(e, TimeoutError) else _describe(e)
        if first:
            self._failing_since = time.time()
        if first or now - self._failure_logged_at >= _FAILURE_LOG_INTERVAL_SECONDS:
            self._failure_logged_at = now
            held = self._snap
            logger.warning(
                "snapshot %s build failed after %d ms: %s — %s; next attempt in %.0f s at the earliest",
                self.name,
                int((now - started) * 1000),
                self._last_error,
                f"still serving the one from {held.age_seconds():.0f} s ago" if held else "nothing to serve yet",
                self.retry_after_failure_seconds,
            )


# ── keep-warm ──────────────────────────────────────────────────────────────
@dataclass
class KeepWarm:
    """How the background refresher looks after one cell.

    It rebuilds the cell when it holds nothing, every `every_seconds`, and —
    when `fingerprint` is given — whenever the fingerprint changes (the
    registry mirror gained, lost or changed an agent), but not more often than
    `min_change_rebuild_seconds`. A cell that should not be built yet says so
    through its own `may_build` gate, which this respects like any reader.
    """

    cell: SnapshotCell[Any]
    every_seconds: float
    fingerprint: Callable[[], Hashable] | None = None
    min_change_rebuild_seconds: float = 0.0
    _built_fingerprint: Hashable | None = field(default=None, init=False)

    def tick(self) -> None:
        cell = self.cell
        snap = cell.current()
        if snap is None or snap.source != "live":
            self._kick()
            return
        age = cell.age_monotonic(snap)
        if age >= self.every_seconds or cell._retired(snap) or cell._expired:
            self._kick()
            return
        if self.fingerprint is not None and age >= self.min_change_rebuild_seconds:
            if self.fingerprint() != self._built_fingerprint:
                self._kick()

    def _kick(self) -> None:
        if self.cell.building():
            return
        # Read before the build starts, so a change landing during it is seen
        # as a change at the next tick; recorded only for a build that started.
        fingerprint = self.fingerprint() if self.fingerprint is not None else None
        if self.cell.refresh() is not None:
            self._built_fingerprint = fingerprint


# Registered by the modules that own each cell, at import.
_schedules: list[KeepWarm] = []

# Startup work registered alongside (restoring persisted snapshots).
_boot_hooks: list[Callable[[], Awaitable[None]]] = []

# Set False to stop `start` from running anything — the hermetic test suite
# does, so a TestClient's lifespan builds nothing behind the test's back.
KEEP_WARM_ENABLED = True

# How often the refresher looks at its cells. Cheap: a few comparisons each.
TICK_SECONDS = 2.0

_loop_task: asyncio.Task[None] | None = None
_boot_tasks: set[asyncio.Task[None]] = set()


def keep_warm(schedule: KeepWarm) -> None:
    """Have the background refresher look after `schedule.cell`."""
    _schedules.append(schedule)


def on_boot(hook: Callable[[], Awaitable[None]]) -> None:
    """Run `hook` in the background once the refresher starts."""
    _boot_hooks.append(hook)


def cells() -> list[SnapshotCell[Any]]:
    return [s.cell for s in _schedules]


async def _refresher() -> None:
    while True:
        for schedule in _schedules:
            try:
                schedule.tick()
            except Exception as e:
                logger.warning("snapshot %s keep-warm tick failed: %s", schedule.cell.name, _describe(e))
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """Start the refresher and the boot hooks. Idempotent; never blocks."""
    global _loop_task
    if not KEEP_WARM_ENABLED or (_loop_task is not None and not _loop_task.done()):
        return
    loop = asyncio.get_running_loop()
    for hook in _boot_hooks:
        task = loop.create_task(_guarded(hook))
        _boot_tasks.add(task)
        task.add_done_callback(_boot_tasks.discard)
    _loop_task = loop.create_task(_refresher(), name="snapshot-refresher")


async def _guarded(hook: Callable[[], Awaitable[None]]) -> None:
    try:
        await hook()
    except Exception as e:
        logger.warning("snapshot boot hook %s failed: %s", getattr(hook, "__name__", hook), _describe(e))


async def stop() -> None:
    """Stop the refresher, the boot hooks and every build in flight (shutdown)."""
    global _loop_task
    tasks = [t for t in (_loop_task, *_boot_tasks) if t is not None and not t.done()]
    for cell in cells():
        running = cell._live_task()
        if running is not None:
            tasks.append(running)
    _loop_task = None
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=5)


def reset_all() -> None:
    """Forget every registered cell's state (tests)."""
    for cell in cells():
        cell.reset()
    for schedule in _schedules:
        schedule._built_fingerprint = None
