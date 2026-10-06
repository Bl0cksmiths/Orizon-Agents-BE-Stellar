"""The task store's SQL against a real Postgres (conftest `pg_dsn`).

A fake can only re-implement what the statements are MEANT to do; these run
the statements themselves: the upsert that keeps a read-token digest, the
trace insert a retry must not duplicate, the bodies Postgres would refuse as
JSONB, the indexes the read paths need, and the retention prune.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from typing import Any, TypeVar

import pytest
from pg_support import execute, fetch

from app.schemas import Plan, PlanStep, StoredPlan, Task, TraceLine
from app.services.task_store import (
    PlanRow,
    PostgresTaskStore,
    TaskRow,
    TaskStoreRejected,
    TraceRow,
    WriteBatch,
)
from app.state import read_token_digest

T = TypeVar("T")
DIGEST = read_token_digest("a-read-token")


def _run(store: PostgresTaskStore, coro: Coroutine[Any, Any, T]) -> T:
    async def once() -> T:
        try:
            return await coro
        finally:
            await store.close()

    return asyncio.run(once())


def _task(task_id: str = "tsk_00000000000000a1", **extra: Any) -> Task:
    fields: dict[str, Any] = {"intent": "build a page", "agents": 2, "spent": 0.03, "status": "complete"}
    return Task(id=task_id, **(fields | extra))


def _row(task: Task, digest: str | None = DIGEST, *, updated_at: float = 100.0) -> TaskRow:
    return TaskRow.of(task, read_token_sha256=digest, boot_id="b" * 16, updated_at=updated_at)


def _line(task_id: str, seq: int, msg: str) -> TraceRow:
    return TraceRow.of(task_id, seq, TraceLine(t=f"00.{seq:03d}", level="exec", msg=msg))


def _plan(plan_id: str, created_at: float) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent="build a page",
        plan=Plan(
            steps=[PlanStep(agent_id="agt_x", agent_name="w.x", rationale="r", est_price_usdc=0.01, est_eta_seconds=1)]
        ),
        total_usdc=0.01,
        total_eta=1.0,
        created_at=created_at,
    )


def test_a_task_round_trips_byte_for_byte_including_text_jsonb_refuses(pg_dsn: str) -> None:
    """An artifact is operator-written text. NUL and a lone surrogate are what
    JSONB refuses; stored as escaped JSON text they come back exactly."""
    hostile = "nul\x00 surrogate\ud800 accents é中 emoji \U0001f600"
    task = _task(artifact={"title": hostile, "files": [{"path": "a.html", "content": hostile}]}, settlement="settled")
    store = PostgresTaskStore(pg_dsn)

    async def go() -> Any:
        await store.write(
            WriteBatch(tasks=(_row(task),), traces=(_line(task.id, 0, hostile), _line(task.id, 1, "sealed")))
        )
        return await store.load_task(task.id)

    stored = _run(store, go())
    assert stored is not None
    assert stored.task == task
    assert stored.task.artifact["title"] == hostile
    assert [line.msg for line in stored.traces] == [hostile, "sealed"]
    assert (stored.read_token_sha256, stored.boot_id) == (DIGEST, "b" * 16)


def test_writes_are_idempotent_and_the_digest_survives_later_snapshots(pg_dsn: str) -> None:
    store = PostgresTaskStore(pg_dsn)
    first = WriteBatch(tasks=(_row(_task(status="running")),), traces=(_line("tsk_00000000000000a1", 0, "one"),))
    later = WriteBatch(tasks=(_row(_task(), digest=None),), traces=(_line("tsk_00000000000000a1", 0, "one"),))

    async def go() -> Any:
        await store.write(first)
        await store.write(first)  # a retry of a batch that had in fact committed
        await store.write(later)
        await store.write(later)
        return await store.load_task("tsk_00000000000000a1")

    stored = _run(store, go())
    assert stored.task.status == "complete"
    assert stored.read_token_sha256 == DIGEST
    assert [line.msg for line in stored.traces] == ["one"]
    assert len(asyncio.run(fetch(pg_dsn, "SELECT * FROM task_records"))) == 1
    assert len(asyncio.run(fetch(pg_dsn, "SELECT * FROM task_trace_lines"))) == 1


def test_a_trace_line_is_activity_on_its_task(pg_dsn: str) -> None:
    """`updated_at` moves with every trace write: it is how a later process
    tells a run still going somewhere from one that died with its process."""
    store = PostgresTaskStore(pg_dsn)
    before = time.time()

    async def go() -> Any:
        await store.write(WriteBatch(tasks=(_row(_task(status="running"), updated_at=1.0),)))
        await store.write(WriteBatch(traces=(_line("tsk_00000000000000a1", 0, "step"),)))
        return await store.load_task("tsk_00000000000000a1")

    assert _run(store, go()).updated_at >= before


def test_a_plan_round_trips_and_a_missing_one_is_none(pg_dsn: str) -> None:
    store = PostgresTaskStore(pg_dsn)
    plan = _plan("pln_0a0b0c0d", created_at=123.0)

    async def go() -> tuple[Any, Any, Any]:
        await store.write(WriteBatch(plans=(PlanRow.of(plan),)))
        return (
            await store.load_plan("pln_0a0b0c0d"),
            await store.load_plan("pln_ffffffff"),
            await store.load_task("tsk_ffffffffffffffff"),
        )

    found, missing_plan, missing_task = _run(store, go())
    assert found == plan
    assert (missing_plan, missing_task) == (None, None)


def test_a_value_the_database_refuses_is_reported_as_refused(pg_dsn: str) -> None:
    """Retrying refused data cannot succeed, so it is told apart from a
    database that is merely unreachable."""
    store = PostgresTaskStore(pg_dsn)
    with pytest.raises(TaskStoreRejected):
        _run(store, store.write(WriteBatch(traces=(TraceRow("tsk_00000000000000a1", 2**31, "{}"),))))


def test_an_unreadable_row_is_absent_rather_than_an_outage_forever(pg_dsn: str) -> None:
    store = PostgresTaskStore(pg_dsn)
    _run(store, store.write(WriteBatch(tasks=(_row(_task()),))))
    asyncio.run(execute(pg_dsn, 'UPDATE task_records SET body = \'{"not": "a task"}\''))
    assert _run(store, store.load_task("tsk_00000000000000a1")) is None


def test_the_schema_is_idempotent_and_indexed_for_its_reads(pg_dsn: str) -> None:
    for _ in range(2):  # two boots against one database
        store = PostgresTaskStore(pg_dsn)
        _run(store, store.load_task("tsk_ffffffffffffffff"))
    indexes = {row["indexname"] for row in asyncio.run(fetch(pg_dsn, "SELECT indexname FROM pg_indexes"))}
    assert {
        "task_records_pkey",
        "task_records_started_at_idx",
        "task_trace_lines_pkey",
        "stored_plans_pkey",
        "stored_plans_created_at_idx",
    } <= indexes


def test_retention_keeps_the_newest_tasks_inside_the_window_with_their_traces(pg_dsn: str) -> None:
    store = PostgresTaskStore(pg_dsn)
    now = 1_000_000.0
    tasks = [
        _task(f"tsk_{i:016x}").model_copy(update={"started_at": started})
        for i, started in enumerate((now - 10, now - 20, now - 30, now - 40_000))
    ]

    async def go() -> Any:
        await store.write(
            WriteBatch(
                tasks=tuple(_row(t) for t in tasks),
                traces=tuple(_line(t.id, 0, "x") for t in tasks) + (_line("tsk_00000000000000ff", 0, "orphan"),),
                plans=(PlanRow.of(_plan("pln_00000001", now - 5)), PlanRow.of(_plan("pln_00000002", now - 90_000))),
            )
        )
        # Inside a 1,000 s window and at most two tasks: the two newest stay.
        return await store.prune(task_cutoff=now - 1_000, max_tasks=2, plan_cutoff=now - 86_400)

    pruned = _run(store, go())
    kept = {row["task_id"] for row in asyncio.run(fetch(pg_dsn, "SELECT task_id FROM task_records"))}
    lines = {row["task_id"] for row in asyncio.run(fetch(pg_dsn, "SELECT task_id FROM task_trace_lines"))}
    plans = {row["plan_id"] for row in asyncio.run(fetch(pg_dsn, "SELECT plan_id FROM stored_plans"))}
    assert kept == {tasks[0].id, tasks[1].id}
    assert lines == kept
    assert plans == {"pln_00000001"}
    assert (pruned.tasks, pruned.trace_lines, pruned.plans) == (2, 3, 1)


def test_recent_task_ids_are_the_newest_by_start_time_and_bounded(pg_dsn: str) -> None:
    """The warm-up's listing: one indexed query, newest first, `limit` rows."""
    store = PostgresTaskStore(pg_dsn)
    tasks = [
        _task(f"tsk_{i:016x}").model_copy(update={"started_at": started})
        for i, started in enumerate((30.0, 10.0, 40.0, 20.0))
    ]

    async def go() -> tuple[list[str], list[str]]:
        await store.write(WriteBatch(tasks=tuple(_row(t) for t in tasks)))
        return await store.recent_task_ids(3), await store.recent_task_ids(0)

    newest, none = _run(store, go())
    assert newest == [tasks[2].id, tasks[0].id, tasks[3].id]
    assert none == []


# ── concurrent first use ──────────────────────────────────────────────────


def test_concurrent_first_uses_create_the_schema_without_a_race(pg_dsn: str) -> None:
    """Several processes creating the schema at once — an old and a new instance
    across a deploy — must queue on the DDL lock, not fail on the catalog's
    unique index (app/services/pg_schema.py)."""

    async def first_uses() -> None:
        stores = [PostgresTaskStore(pg_dsn) for _ in range(8)]
        try:
            await asyncio.gather(*(store._ready_pool() for store in stores))
        finally:
            for store in stores:
                await store.close()

    asyncio.run(first_uses())
