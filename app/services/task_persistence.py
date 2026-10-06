"""AppState as a hot cache in front of the durable task store (D-090).

Two halves, one module, because they share one rule — the process's memory is
the cache and `task_store` is the record:

**Writing.** Every task, trace line, read token and plan AppState is given is
reported here (`state.StateObserver`) and written to Postgres by ONE background
worker per process. The report is synchronous and only records the change, so
nothing on a request path or in the run loop ever waits on the database:

  * Coalesced. A task is upserted as its LATEST snapshot — ten settlement and
    status updates between two writes cost one row write — and a burst of trace
    lines goes out as one batch, in one transaction, a few milliseconds later.
  * Idempotent. Task and plan rows are keyed upserts; a trace line is keyed by
    its index in its task's trace and inserted ON CONFLICT DO NOTHING. A batch
    retried after a timeout that actually committed changes nothing.
  * Retried, bounded. A failed batch goes back into the queue (newer snapshots
    win) and is retried with exponential backoff, so a database that is briefly
    down or still waking loses nothing. The queue is capped
    (`MAX_PENDING_TASKS`, `MAX_PENDING_TRACE_LINES`); past the cap the oldest
    pending writes are dropped and that is logged at ERROR, rather than a
    long outage growing the process until Render kills it. A batch the
    database REFUSES as data is split, and only the rows it refuses are
    dropped (ERROR) — a poison row must not block every write behind it.
  * Write-through where a receipt depends on it. `flush()` waits, bounded, for
    everything reported so far: `/decompose` and `/execute` await it before
    answering, so the plan id, task id and read token they hand back are
    already durable, and a run awaits it once it ends, so its terminal state is.

**Reading.** `ensure_task` and `load_plan` serve from memory and fall back to
the store on a miss — a restart, or eviction from the 200-task window. A
hydrated task is held like any other; its read token comes back as a digest,
which `task_auth` checks without ever holding the token (`state.read_token_digest`).
A read the store cannot answer within `READ_TIMEOUT_SECONDS` raises
`TaskStoreUnavailable`, which the routes answer 503 — never a hang, and never a
404 that would tell a buyer their paid run does not exist. Misses are cached
briefly, so a flood of made-up ids costs the database one query each per
`NEGATIVE_TTL_SECONDS`, and ids that cannot have been minted here never reach it.

A run is executed by exactly one process, so a task that another process left
non-terminal is either still running there (Render overlaps the old and new
instance during a deploy) or died with it (a restart mid-run). The first is
re-read from the store at most every `FOREIGN_REFRESH_SECONDS` rather than
served stale forever; the second — no write for `ORPHAN_AFTER_SECONDS`, longer
than any run's longest silence — is closed as failed, with a trace line saying
why, instead of reading "running" forever.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..schemas import StoredPlan, Task, TraceLine
from ..state import read_token_digest, set_observer, state
from ..trace_bus import bus
from . import task_store
from .task_store import PlanRow, StoredTask, TaskRow, TaskStoreRejected, TraceRow, WriteBatch

logger = logging.getLogger(__name__)

# Who wrote a row. Drawn once per process, so a reader can tell a task this
# process is running from one a previous process left behind.
BOOT_ID = secrets.token_hex(8)

# Reads on a request path: long enough for a suspended Neon compute to wake
# (well under a second, typically) and for one connect retry; short enough that
# a database that is down is a prompt 503, not a request that hangs.
READ_TIMEOUT_SECONDS = 6.0

# Writes: how long a burst of reports is allowed to gather before it is sent,
# and how the retry interval grows while the database is unreachable.
WRITE_BATCH_DELAY_SECONDS = 0.02
RETRY_INITIAL_SECONDS = 0.5
RETRY_MAX_SECONDS = 30.0

# What `/decompose` and `/execute` wait for the plan or task id they hand out
# to be durable before answering, and what a finished run waits for its final
# state. Bounded: a slow database delays durability, never the buyer's answer
# by more than this.
RESPONSE_FLUSH_SECONDS = 2.0
RUN_END_FLUSH_SECONDS = 5.0

# The write-behind queue's ceiling while the database is unreachable. A task
# row carries its artifact (~70 KB at most), so the worst case is ~70 MB of
# tasks plus the lines — survivable on a free instance, and a database down
# that long has bigger news than a dropped trace line.
MAX_PENDING_TASKS = 1_000
MAX_PENDING_TRACE_LINES = 20_000
MAX_PENDING_PLANS = 1_000

# How long "that id is not in the store" is believed, and for how many ids.
NEGATIVE_TTL_SECONDS = 5.0
NEGATIVE_CACHE_MAX = 1_024

# Another process's non-terminal task: re-read at most this often...
FOREIGN_REFRESH_SECONDS = 2.0
# ...and given up for dead after this long without a write. A live run writes
# a trace line around every step; its longest silence is one step's ceiling
# (execution_svc.STEP_TIMEOUT_SECONDS, 120 s) or the settle's allowance
# (SETTLE_ALLOWANCE_SECONDS, 150 s). Ten minutes is several of either.
ORPHAN_AFTER_SECONDS = 600.0
INTERRUPTED_MESSAGE = "workflow interrupted — the backend restarted before this run finished"

# How often the worker applies the retention policy (task_store module docstring).
PRUNE_EVERY_SECONDS = 3_600.0

# The ids this service mints (execution_svc.execute_plan, orchestrator_svc).
# Anything else was never written here, so it is never looked up.
_TASK_ID = re.compile(r"^tsk_[0-9a-f]{16}$")
_PLAN_ID = re.compile(r"^pln_[0-9a-f]{8}$")

_TERMINAL = frozenset({"complete", "failed"})


class TaskStoreUnavailable(Exception):
    """The store could not answer a read in time, so whether the task (or plan)
    exists is unknown. Routes answer 503 — retryable — never 404."""


@dataclass
class _Pending:
    tasks: dict[str, TaskRow] = field(default_factory=dict)
    traces: dict[tuple[str, int], TraceRow] = field(default_factory=dict)
    plans: dict[str, PlanRow] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.tasks or self.traces or self.plans)

    def batch(self) -> WriteBatch:
        return WriteBatch(tuple(self.tasks.values()), tuple(self.traces.values()), tuple(self.plans.values()))


class TaskJournal:
    """The write-behind queue and its single worker. Implements `StateObserver`."""

    def __init__(self) -> None:
        self._pending = _Pending()
        # Reports recorded / reports durably written, as monotone counters:
        # `flush` waits for `_written` to reach the `_recorded` it saw.
        self._recorded = 0
        self._written = 0
        self._waiters: list[tuple[int, asyncio.Future[None]]] = []
        self._worker: asyncio.Task[None] | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0
        self._failures = 0
        self._last_prune = 0.0

    # ── StateObserver ────────────────────────────────────────────
    def task_saved(self, task: Task, read_token: str | None) -> None:
        if task_store.get_task_store() is None:
            return
        previous = self._pending.tasks.pop(task.id, None)
        digest = read_token_digest(read_token) if read_token is not None else None
        if digest is None and previous is not None:
            # A snapshot coalesced over the creation row must keep its digest.
            digest = previous.read_token_sha256
        self._pending.tasks[task.id] = TaskRow.of(
            task, read_token_sha256=digest, boot_id=BOOT_ID, updated_at=time.time()
        )
        self._cap(self._pending.tasks, MAX_PENDING_TASKS, "task snapshots")
        self._recorded += 1
        self._kick()

    def trace_appended(self, task_id: str, seq: int, line: TraceLine) -> None:
        if task_store.get_task_store() is None:
            return
        self._pending.traces[(task_id, seq)] = TraceRow.of(task_id, seq, line)
        self._cap(self._pending.traces, MAX_PENDING_TRACE_LINES, "trace lines")
        self._recorded += 1
        self._kick()

    def plan_saved(self, plan: StoredPlan) -> None:
        if task_store.get_task_store() is None:
            return
        self._pending.plans.pop(plan.id, None)
        self._pending.plans[plan.id] = PlanRow.of(plan)
        self._cap(self._pending.plans, MAX_PENDING_PLANS, "plans")
        self._recorded += 1
        self._kick()

    def _cap(self, pending: dict[Any, Any], limit: int, what: str) -> None:
        while len(pending) > limit:
            pending.pop(next(iter(pending)))
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1_000 == 0:
                logger.error(
                    "task store: write queue full while the database is unreachable — dropped the oldest pending"
                    " %s (%d dropped so far); those writes are lost",
                    what,
                    self._dropped,
                )

    # ── the worker ───────────────────────────────────────────────
    def _kick(self) -> None:
        """Start the worker if none is running. Needs a running loop in this
        thread; a report made outside one is written by the next kick or flush."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        worker = self._worker
        if worker is not None and not worker.done():
            owner = self._worker_loop
            if owner is loop or (owner is not None and not owner.is_closed() and owner.is_running()):
                return
        self._worker = loop.create_task(self._drain(), name="task-store-writer")
        self._worker_loop = loop

    async def _drain(self) -> None:
        delay = WRITE_BATCH_DELAY_SECONDS
        while True:
            await asyncio.sleep(delay)
            store = task_store.get_task_store()
            if store is None:
                # DATABASE_URL was cleared: nothing will ever write these.
                self._pending = _Pending()
                self._settle(self._recorded)
                return
            if not self._pending:
                self._settle(self._recorded)
                return
            pending, upto = self._pending, self._recorded
            self._pending = _Pending()
            try:
                await self._write(store, pending)
            except asyncio.CancelledError:
                self._restore(pending)
                raise
            except Exception as exc:
                self._restore(pending)
                self._failures += 1
                delay = min(RETRY_MAX_SECONDS, RETRY_INITIAL_SECONDS * 2 ** (self._failures - 1))
                logger.warning(
                    "task store: write of %d task(s), %d trace line(s), %d plan(s) failed (attempt %d), retrying in"
                    " %.1fs: %r",
                    len(pending.tasks),
                    len(pending.traces),
                    len(pending.plans),
                    self._failures,
                    delay,
                    exc,
                )
                continue
            if self._failures:
                logger.info("task store: writes recovered after %d failed attempt(s)", self._failures)
            self._failures = 0
            self._dropped = 0
            delay = WRITE_BATCH_DELAY_SECONDS
            self._settle(upto)
            await self._maybe_prune(store)

    async def _write(self, store: task_store.TaskStore, pending: _Pending) -> None:
        try:
            await store.write(pending.batch())
            return
        except TaskStoreRejected:
            logger.warning("task store: a batch was refused as data; writing its rows one by one")
        # One row per write, so only the rows the database refuses are lost.
        singles = (
            [WriteBatch(plans=(p,)) for p in pending.plans.values()]
            + [WriteBatch(tasks=(t,)) for t in pending.tasks.values()]
            + [WriteBatch(traces=(r,)) for r in pending.traces.values()]
        )
        for single in singles:
            try:
                await store.write(single)
            except TaskStoreRejected as exc:
                row = (single.plans or single.tasks or single.traces)[0]
                logger.error("task store: dropped a row the database refuses (%r): %s", row, exc)

    def _restore(self, failed: _Pending) -> None:
        """Put a failed batch back, without overwriting anything newer."""
        for key, row in failed.tasks.items():
            newer = self._pending.tasks.get(key)
            if newer is None:
                self._pending.tasks[key] = row
            elif newer.read_token_sha256 is None and row.read_token_sha256 is not None:
                self._pending.tasks[key] = TaskRow(
                    newer.task_id,
                    newer.body,
                    newer.status,
                    newer.started_at,
                    row.read_token_sha256,
                    newer.boot_id,
                    newer.updated_at,
                )
        for trace_key, trace in failed.traces.items():
            self._pending.traces.setdefault(trace_key, trace)
        for plan_key, plan in failed.plans.items():
            self._pending.plans.setdefault(plan_key, plan)

    def _settle(self, upto: int) -> None:
        self._written = max(self._written, upto)
        still: list[tuple[int, asyncio.Future[None]]] = []
        for target, waiter in self._waiters:
            if target <= self._written:
                _resolve(waiter)
            else:
                still.append((target, waiter))
        self._waiters = still

    async def _maybe_prune(self, store: task_store.TaskStore) -> None:
        now = time.time()
        if now - self._last_prune < PRUNE_EVERY_SECONDS:
            return
        self._last_prune = now
        try:
            pruned = await store.prune(
                task_cutoff=now - task_store.TASK_RETENTION_SECONDS,
                max_tasks=task_store.TASK_RETENTION_MAX,
                plan_cutoff=now - max(task_store.PLAN_RETENTION_SECONDS, 2 * settings.plan_ttl_seconds),
            )
        except Exception as exc:
            logger.warning("task store: retention prune failed, next attempt in an hour: %r", exc)
            return
        if pruned.tasks or pruned.trace_lines or pruned.plans:
            logger.info(
                "task store: retention pruned %d task(s), %d trace line(s), %d plan(s)",
                pruned.tasks,
                pruned.trace_lines,
                pruned.plans,
            )

    async def flush(self, timeout: float) -> bool:
        """Wait, at most `timeout` seconds, until everything reported before
        this call is durably written. True when it is (or there is no store)."""
        if task_store.get_task_store() is None:
            return True
        target = self._recorded
        if self._written >= target:
            return True
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((target, waiter))
        self._kick()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except TimeoutError:
            return False
        finally:
            self._waiters = [(t, w) for t, w in self._waiters if w is not waiter]
        return True

    @property
    def pending_writes(self) -> int:
        return len(self._pending.tasks) + len(self._pending.traces) + len(self._pending.plans)


def _resolve(waiter: asyncio.Future[None]) -> None:
    """Resolve a flush waiter from whichever thread the worker runs on."""

    def _set() -> None:
        if not waiter.done():
            waiter.set_result(None)

    loop = waiter.get_loop()
    if loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _set()
    else:
        loop.call_soon_threadsafe(_set)


journal = TaskJournal()
set_observer(journal)


async def flush(timeout: float) -> bool:
    """`journal.flush` — see `TaskJournal.flush`."""
    return await journal.flush(timeout)


# ── reading ──────────────────────────────────────────────────────

_misses: OrderedDict[str, float] = OrderedDict()
# Hydrated tasks another process may still be running: id → when last read.
_foreign: dict[str, float] = {}


def _missed_recently(task_id: str) -> bool:
    expires = _misses.get(task_id)
    if expires is None:
        return False
    if expires > time.monotonic():
        return True
    _misses.pop(task_id, None)
    return False


def _remember_miss(task_id: str) -> None:
    _misses[task_id] = time.monotonic() + NEGATIVE_TTL_SECONDS
    _misses.move_to_end(task_id)
    while len(_misses) > NEGATIVE_CACHE_MAX:
        _misses.popitem(last=False)


async def ensure_task(task_id: str) -> bool:
    """True when `task_id` is held in `state` — already, or hydrated now from
    the store. Raises `TaskStoreUnavailable` when the store cannot say."""
    held = task_id in state.tasks
    if held and task_id not in _foreign:
        return True
    store = task_store.get_task_store()
    if store is None or not _TASK_ID.match(task_id):
        return held
    if held and time.monotonic() - _foreign[task_id] < FOREIGN_REFRESH_SECONDS:
        return True
    if not held and _missed_recently(task_id):
        return False
    try:
        stored = await asyncio.wait_for(store.load_task(task_id), READ_TIMEOUT_SECONDS)
    except Exception as exc:
        if held:
            # A refresh of a task already held: the copy in hand is the best
            # answer available, and a 503 for it would be worse than stale.
            logger.debug("task store: refresh of %s failed, serving the held copy: %r", task_id, exc)
            _foreign[task_id] = time.monotonic()
            return True
        logger.warning("task store: read of %s failed: %r", task_id, exc)
        raise TaskStoreUnavailable(task_id) from exc
    if stored is None:
        if not held:
            _remember_miss(task_id)
        return held
    await _absorb(stored, replace=held)
    return True


async def _absorb(stored: StoredTask, *, replace: bool) -> None:
    task = stored.task
    foreign = stored.boot_id != BOOT_ID and task.status not in _TERMINAL
    if replace:
        state.tasks[task.id] = task
        state.traces[task.id] = list(stored.traces)
    else:
        state.hydrate_task(task, stored.read_token_sha256, list(stored.traces))
    _foreign.pop(task.id, None)
    if not foreign:
        return
    if time.time() - stored.updated_at <= ORPHAN_AFTER_SECONDS:
        _foreign[task.id] = time.monotonic()
        return
    # Nobody has written this run for longer than any run stays silent: the
    # process that ran it is gone. Close it, durably, and say why.
    logger.warning(
        "task %s: left %s by process %s with no write for %.0fs — closed as failed (interrupted by a restart)",
        task.id,
        task.status,
        stored.boot_id,
        time.time() - stored.updated_at,
    )
    elapsed = max(stored.updated_at - task.started_at, 0.0)
    seconds, millis = divmod(int(elapsed * 1000), 1000)
    state.put_task(task.model_copy(update={"status": "failed"}))
    state.append_trace(task.id, TraceLine(t=f"{seconds:02d}.{millis:03d}", level="error", msg=INTERRUPTED_MESSAGE))
    await bus.close(task.id)


async def load_plan(plan_id: str) -> StoredPlan | None:
    """The plan, from memory or hydrated from the store; None when neither has
    it. Raises `TaskStoreUnavailable` when the store cannot say."""
    plan = state.plans.get(plan_id)
    if plan is not None:
        return plan
    store = task_store.get_task_store()
    if store is None or not _PLAN_ID.match(plan_id):
        return None
    try:
        stored = await asyncio.wait_for(store.load_plan(plan_id), READ_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("task store: read of plan %s failed: %r", plan_id, exc)
        raise TaskStoreUnavailable(plan_id) from exc
    if stored is None:
        return None
    state.hydrate_plan(stored)
    return state.plans.get(plan_id, stored)


async def close(timeout: float = 10.0) -> None:
    """Flush what is pending (bounded), then release the store's pool.

    For the lifespan shutdown, AFTER in-flight runs are drained, so their final
    states are among what is flushed.
    """
    if not await journal.flush(timeout):
        logger.error(
            "task store: %d write(s) still pending at shutdown were not persisted",
            journal.pending_writes,
        )
    await task_store.close_task_store()
