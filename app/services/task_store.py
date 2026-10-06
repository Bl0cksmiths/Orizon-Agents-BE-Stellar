"""Where tasks, their traces and stored plans live — durably (D-090).

`AppState` holds every task in process memory, and Render's free instance
restarts whenever it idles, so a receipt link (`/api/tasks/{id}`) opened after
a restart answered 404 `unknown_task` for a run that had settled and been paid
for. The settlement survived — `dispute_store` has kept it in Postgres since
story 4.02 — but the task, its trace, its artifact and its read token did not.
This module is the durable half; `services/task_persistence.py` keeps AppState
as the hot cache in front of it and does the writing (write-behind, coalesced,
retried) and the reading (on a cache miss).

The shape follows `dispute_store.py` and `binding_store.py` deliberately: the
same lazily imported driver, the same lazily dialled pool with the same three
timeouts, the same idempotent DDL instead of a migration framework, and epoch
seconds from our own clock. Three tables:

  * `task_records` — one row per task, UPSERTED: a task is mutable state (its
    status, spend, settlement and artifact change as the run goes), and the
    row is its latest snapshot, keyed by task id so a retried write is the
    same write. The read token is stored as its SHA-256 digest only
    (`state.read_token_digest`): a row read out of the database is never a
    working credential.
  * `task_trace_lines` — one row per trace line, keyed (task_id, seq) where
    `seq` is the line's index in the task's trace. Append-only and written
    with ON CONFLICT DO NOTHING, so a retried batch never duplicates a line.
  * `stored_plans` — one row per plan the planner returned, so a buyer who
    authorised a plan just before a restart can still execute it inside its
    TTL instead of having their custody released as `plan_unknown`.

Bodies are TEXT holding ASCII-escaped JSON rather than JSONB. JSONB refuses
`\\u0000` and lone surrogates, and an artifact is operator- or LLM-written text
that can contain either; a row the database refuses would be retried forever
or dropped. ASCII-escaped JSON is storable whatever the text holds and reads
back byte-identical through `json.loads`. Postgres compresses the large
values (a ~70 KB artifact) itself, out of line (TOAST).

Retention (`prune`, run by the persistence worker at most hourly): a task and
its trace are kept for `TASK_RETENTION_SECONDS` (30 days) and at most the
newest `TASK_RETENTION_MAX` (2,000) tasks, whichever is smaller — sized so the
worst case (every task carrying a full artifact) stays far inside a free Neon
branch. A plan is kept for `PLAN_RETENTION_SECONDS` (a day), well past any TTL
an execute would still honour. Settlements and disputes are NOT pruned here:
they are money records with their own store and their own rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import settings
from ..schemas import StoredPlan, Task, TraceLine
from .pg_schema import create_schema

logger = logging.getLogger(__name__)

# Retention — see the module docstring. Module constants rather than settings:
# they bound storage, not behaviour, and a deployment that needs another number
# needs a decision recorded with it, not an env var.
TASK_RETENTION_SECONDS = 30 * 86_400
TASK_RETENTION_MAX = 2_000
PLAN_RETENTION_SECONDS = 86_400

# Pool sizing and timeouts, for dispute_store's reasons (min_size=0 because a
# free instance wakes holding dead sockets; small because --workers 1 makes
# this the whole service's budget, spent alongside the binding and dispute
# pools). Three rather than five: one writer and the occasional read.
_POOL_MIN_SIZE = 0
_POOL_MAX_SIZE = 3
_POOL_CONNECT_TIMEOUT = 10.0
_POOL_COMMAND_TIMEOUT = 10.0
_POOL_ACQUIRE_TIMEOUT = 15.0

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS task_records (
    task_id           TEXT PRIMARY KEY,
    body              TEXT NOT NULL,
    status            TEXT NOT NULL,
    started_at        DOUBLE PRECISION NOT NULL,
    read_token_sha256 TEXT,
    boot_id           TEXT NOT NULL,
    updated_at        DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS task_records_started_at_idx ON task_records (started_at DESC);
CREATE TABLE IF NOT EXISTS task_trace_lines (
    task_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    line    TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS stored_plans (
    plan_id    TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS stored_plans_created_at_idx ON stored_plans (created_at);
"""

# The newest snapshot wins. The digest is COALESCEd rather than overwritten: it
# is written once, with the task's first snapshot, and every later snapshot
# carries None — which must never erase it.
_UPSERT_TASK_SQL = """
INSERT INTO task_records (task_id, body, status, started_at, read_token_sha256, boot_id, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (task_id) DO UPDATE SET
    body = EXCLUDED.body,
    status = EXCLUDED.status,
    started_at = EXCLUDED.started_at,
    read_token_sha256 = COALESCE(EXCLUDED.read_token_sha256, task_records.read_token_sha256),
    boot_id = EXCLUDED.boot_id,
    updated_at = EXCLUDED.updated_at
"""

_INSERT_TRACE_SQL = """
INSERT INTO task_trace_lines (task_id, seq, line) VALUES ($1, $2, $3)
ON CONFLICT (task_id, seq) DO NOTHING
"""

# A trace line is activity on its task. `updated_at` is what tells a later
# process whether a run it finds "running" is still going somewhere or died
# with the process that ran it (task_persistence.ORPHAN_AFTER_SECONDS).
_TOUCH_SQL = """
UPDATE task_records SET updated_at = GREATEST(updated_at, $2) WHERE task_id = ANY($1::text[])
"""

_UPSERT_PLAN_SQL = """
INSERT INTO stored_plans (plan_id, body, created_at) VALUES ($1, $2, $3)
ON CONFLICT (plan_id) DO UPDATE SET body = EXCLUDED.body, created_at = EXCLUDED.created_at
"""

_SELECT_TASK_SQL = """
SELECT body, read_token_sha256, boot_id, updated_at FROM task_records WHERE task_id = $1
"""

_SELECT_TRACE_SQL = """
SELECT seq, line FROM task_trace_lines WHERE task_id = $1 ORDER BY seq
"""

_SELECT_PLAN_SQL = "SELECT body FROM stored_plans WHERE plan_id = $1"

# The newest tasks by start time, for the boot warm-up (services/task_warmup.py).
# `task_records_started_at_idx` serves the ORDER BY ... LIMIT as an index scan.
_SELECT_RECENT_IDS_SQL = "SELECT task_id FROM task_records ORDER BY started_at DESC LIMIT $1"

# Past the age limit, or past the newest TASK_RETENTION_MAX by start time.
_PRUNE_TASKS_SQL = """
DELETE FROM task_records
WHERE task_id IN (
    SELECT task_id FROM task_records WHERE started_at < $1
    UNION
    SELECT task_id FROM (
        SELECT task_id FROM task_records ORDER BY started_at DESC OFFSET $2
    ) AS beyond_the_cap
)
"""

# Every trace line whose task is gone: the tasks the statement above removed,
# and any line whose task row was never written at all.
_PRUNE_TRACES_SQL = """
DELETE FROM task_trace_lines AS l
WHERE NOT EXISTS (SELECT 1 FROM task_records AS t WHERE t.task_id = l.task_id)
"""

_PRUNE_PLANS_SQL = "DELETE FROM stored_plans WHERE created_at < $1"


def _import_asyncpg() -> Any:
    """Import the driver at first Postgres use, never at module import —
    `dispute_store._import_asyncpg`'s reason: it is optional without
    DATABASE_URL, and a missing one should say so where it is needed."""
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "DATABASE_URL is set but asyncpg is not installed, so tasks cannot be stored durably. "
            "Install it (`pip install -r requirements.txt`, asyncpg>=0.30,<1) or clear DATABASE_URL."
        ) from exc
    return asyncpg


def _dumps(value: Any) -> str:
    # ASCII-escaped: see the module docstring for why a body is never JSONB.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True)
class TaskRow:
    """One task snapshot as `task_records` stores it."""

    task_id: str
    body: str
    status: str
    started_at: float
    read_token_sha256: str | None
    boot_id: str
    updated_at: float

    @classmethod
    def of(cls, task: Task, *, read_token_sha256: str | None, boot_id: str, updated_at: float) -> TaskRow:
        # `started` is derived at serialization time from `started_at`, so it
        # is never stored — a stored "2m ago" would be wrong a minute later.
        body = _dumps(task.model_dump(mode="json", exclude={"started"}))
        return cls(task.id, body, task.status, task.started_at, read_token_sha256, boot_id, updated_at)


@dataclass(frozen=True)
class TraceRow:
    """One trace line, at its index in its task's trace."""

    task_id: str
    seq: int
    line: str

    @classmethod
    def of(cls, task_id: str, seq: int, line: TraceLine) -> TraceRow:
        return cls(task_id, seq, _dumps(line.model_dump(mode="json")))


@dataclass(frozen=True)
class PlanRow:
    """One stored plan."""

    plan_id: str
    body: str
    created_at: float

    @classmethod
    def of(cls, plan: StoredPlan) -> PlanRow:
        return cls(plan.id, _dumps(plan.model_dump(mode="json")), plan.created_at)


@dataclass(frozen=True)
class WriteBatch:
    """Everything one write sends, applied in ONE transaction."""

    tasks: tuple[TaskRow, ...] = ()
    traces: tuple[TraceRow, ...] = ()
    plans: tuple[PlanRow, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tasks or self.traces or self.plans)


@dataclass(frozen=True)
class StoredTask:
    """A task as the store holds it: its latest snapshot, its whole trace,
    the digest of its read token, and who last wrote it and when."""

    task: Task
    read_token_sha256: str | None
    traces: tuple[TraceLine, ...]
    boot_id: str
    updated_at: float


@dataclass(frozen=True)
class PruneResult:
    tasks: int
    trace_lines: int
    plans: int


class TaskStoreRejected(Exception):
    """The database refused the DATA itself (a constraint or a value it cannot
    store), so sending the same rows again cannot succeed. Distinct from every
    other failure — a timeout, a dropped connection, a database still waking —
    which is retried."""


class TaskStore(Protocol):
    async def write(self, batch: WriteBatch) -> None:
        """Apply every row in `batch` atomically. Idempotent: the same batch
        twice leaves the same rows as once."""
        ...

    async def load_task(self, task_id: str) -> StoredTask | None: ...

    async def load_plan(self, plan_id: str) -> StoredPlan | None: ...

    async def recent_task_ids(self, limit: int) -> list[str]:
        """The newest `limit` task ids by start time, newest first."""
        ...

    async def prune(self, *, task_cutoff: float, max_tasks: int, plan_cutoff: float) -> PruneResult: ...

    async def close(self) -> None: ...


def _count(status: str) -> int:
    """The row count from an asyncpg command tag such as `DELETE 3`."""
    tail = status.rsplit(" ", 1)[-1]
    return int(tail) if tail.isdigit() else 0


class PostgresTaskStore:
    """Tasks, traces and plans in Postgres.

    The pool and the schema are created LAZILY, on the first call that needs
    them, so constructing the store does no I/O and a database briefly
    unreachable at boot costs a failed write (retried) or a 503 on one read,
    never a failed deploy.

    The pool belongs to the event loop that dialled it. A call from another
    loop — the suite's `asyncio.run` per test, a TestClient's portal thread —
    dials a fresh pool instead of handing that loop a pool it cannot use. In
    production there is one loop and this never happens.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: Any | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _loop_lock(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def _ready_pool(self) -> Any:
        loop = asyncio.get_running_loop()
        pool = self._pool
        if pool is not None and self._pool_loop is loop:
            return pool
        async with self._loop_lock(loop):
            if self._pool is not None and self._pool_loop is loop:
                return self._pool
            # A pool from another loop is dropped, not closed: closing awaits
            # its connections on the loop that owns them, which may be gone.
            self._pool, self._pool_loop = None, None
            asyncpg = _import_asyncpg()
            pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=_POOL_MIN_SIZE,
                max_size=_POOL_MAX_SIZE,
                timeout=_POOL_CONNECT_TIMEOUT,
                command_timeout=_POOL_COMMAND_TIMEOUT,
            )
            try:
                await create_schema(
                    pool,
                    "task_records",
                    _CREATE_SQL,
                    timeout=_POOL_COMMAND_TIMEOUT,
                    acquire_timeout=_POOL_ACQUIRE_TIMEOUT,
                )
            except BaseException:
                await pool.close()
                raise
            self._pool, self._pool_loop = pool, loop
            return pool

    async def write(self, batch: WriteBatch) -> None:
        if not batch:
            return
        asyncpg = _import_asyncpg()
        pool = await self._ready_pool()
        try:
            async with pool.acquire(timeout=_POOL_ACQUIRE_TIMEOUT) as conn, conn.transaction():
                if batch.plans:
                    await conn.executemany(
                        _UPSERT_PLAN_SQL,
                        [(p.plan_id, p.body, p.created_at) for p in batch.plans],
                        timeout=_POOL_COMMAND_TIMEOUT,
                    )
                if batch.tasks:
                    await conn.executemany(
                        _UPSERT_TASK_SQL,
                        [
                            (t.task_id, t.body, t.status, t.started_at, t.read_token_sha256, t.boot_id, t.updated_at)
                            for t in batch.tasks
                        ],
                        timeout=_POOL_COMMAND_TIMEOUT,
                    )
                if batch.traces:
                    await conn.executemany(
                        _INSERT_TRACE_SQL,
                        [(r.task_id, r.seq, r.line) for r in batch.traces],
                        timeout=_POOL_COMMAND_TIMEOUT,
                    )
                    await conn.execute(
                        _TOUCH_SQL,
                        sorted({r.task_id for r in batch.traces}),
                        time.time(),
                        timeout=_POOL_COMMAND_TIMEOUT,
                    )
        except (asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError) as exc:
            raise TaskStoreRejected(str(exc)) from exc

    async def load_task(self, task_id: str) -> StoredTask | None:
        pool = await self._ready_pool()
        async with pool.acquire(timeout=_POOL_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(_SELECT_TASK_SQL, task_id, timeout=_POOL_COMMAND_TIMEOUT)
            if row is None:
                return None
            lines = await conn.fetch(_SELECT_TRACE_SQL, task_id, timeout=_POOL_COMMAND_TIMEOUT)
        try:
            task = Task.model_validate(json.loads(row["body"]))
            traces = tuple(TraceLine.model_validate(json.loads(line["line"])) for line in lines)
        except ValueError:
            # A row this code cannot read back (a schema it predates, a body
            # damaged by hand) is reported and treated as absent: answering
            # 503 for it would answer 503 for it forever.
            logger.exception("task store: task %s has a row that cannot be read back; treating it as absent", task_id)
            return None
        return StoredTask(
            task=task,
            read_token_sha256=row["read_token_sha256"],
            traces=traces,
            boot_id=row["boot_id"],
            updated_at=float(row["updated_at"]),
        )

    async def load_plan(self, plan_id: str) -> StoredPlan | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_PLAN_SQL, plan_id, timeout=_POOL_COMMAND_TIMEOUT)
        if row is None:
            return None
        try:
            return StoredPlan.model_validate(json.loads(row["body"]))
        except ValueError:
            logger.exception("task store: plan %s has a row that cannot be read back; treating it as absent", plan_id)
            return None

    async def recent_task_ids(self, limit: int) -> list[str]:
        if limit <= 0:
            return []
        pool = await self._ready_pool()
        rows = await pool.fetch(_SELECT_RECENT_IDS_SQL, limit, timeout=_POOL_COMMAND_TIMEOUT)
        return [str(row["task_id"]) for row in rows]

    async def prune(self, *, task_cutoff: float, max_tasks: int, plan_cutoff: float) -> PruneResult:
        pool = await self._ready_pool()
        async with pool.acquire(timeout=_POOL_ACQUIRE_TIMEOUT) as conn:
            tasks = await conn.execute(_PRUNE_TASKS_SQL, task_cutoff, max_tasks, timeout=_POOL_COMMAND_TIMEOUT)
            lines = await conn.execute(_PRUNE_TRACES_SQL, timeout=_POOL_COMMAND_TIMEOUT)
            plans = await conn.execute(_PRUNE_PLANS_SQL, plan_cutoff, timeout=_POOL_COMMAND_TIMEOUT)
        return PruneResult(tasks=_count(tasks), trace_lines=_count(lines), plans=_count(plans))

    async def close(self) -> None:
        pool, loop = self._pool, self._pool_loop
        self._pool, self._pool_loop = None, None
        if pool is None:
            return
        try:
            same_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            await pool.close()


_store: PostgresTaskStore | None = None
_reported_memory_only = False


def get_task_store() -> TaskStore | None:
    """The durable task store, or None when DATABASE_URL is unset.

    Resolved on every call from the CURRENT `database_url` — the singleton is
    rebuilt when the DSN changes — so a DATABASE_URL set after import is
    honoured rather than pinned at whatever the first caller saw
    (`binding_store.get_binding_store`'s reason). None is a real answer: AppState
    is then the only copy, which local development and the hermetic suite want.
    """
    global _store, _reported_memory_only
    dsn = settings.database_url.strip()
    if not dsn:
        if not _reported_memory_only:
            _reported_memory_only = True
            logger.warning(
                "task store: in-memory only (DATABASE_URL is unset) — tasks, traces and plans are LOST on "
                "restart; set DATABASE_URL to persist them"
            )
        return None
    if _store is None or _store.dsn != dsn:
        _store = PostgresTaskStore(dsn)
        # The DSN carries the database password: report the choice, never the value.
        logger.info("task store: postgres (DATABASE_URL is set) — tasks, traces and plans survive a restart")
    return _store


async def close_task_store() -> None:
    """Close the pool and clear the singleton, so the next call rebuilds it."""
    global _store
    store, _store = _store, None
    if store is not None:
        await store.close()
