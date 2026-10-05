"""Warm the task cache from the durable store after a restart, behind boot.

`task_persistence` reads a task back from the store when a request names it,
but nothing names the newest tasks after a restart: `GET /api/tasks` and the
overview's completion rate read whatever `state` holds, and a fresh process
holds nothing — so both were empty after every wake from idle, which on the
free tier is every visit after a quiet spell.

So boot loads the newest `WARM_TASKS` (the size of the in-memory window) in
the background, each through `task_persistence.ensure_task` — the module that
owns hydration, read-token digests and closing runs a dead process left
behind — at most `WARM_CONCURRENCY` at a time so the journal's writer keeps a
connection, and within `WARM_BUDGET_SECONDS` overall. It never holds a request
and never raises: a store that cannot answer is logged once and left to the
read-through path, which still serves any task asked for by id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..state import state
from . import task_persistence, task_store

logger = logging.getLogger(__name__)

# The in-memory window: loading more would only evict what was just loaded.
WARM_TASKS = state.task_order.maxlen or 200
WARM_CONCURRENCY = 2
WARM_BUDGET_SECONDS = 60.0
_LIST_TIMEOUT_SECONDS = 10.0

# The newest tasks by start time; `task_records_started_at_idx` serves it.
_RECENT_IDS_SQL = "SELECT task_id FROM task_records ORDER BY started_at DESC LIMIT $1"


async def _recent_ids(store: Any, limit: int) -> list[str]:
    """The newest `limit` task ids, newest first.

    Through the store's own `recent_task_ids` when it has one; otherwise, for
    the Postgres store, one indexed query on its pool (so the warm-up also
    dials the pool the journal's writer is about to need).
    """
    lister: Callable[[int], Awaitable[list[str]]] | None = getattr(store, "recent_task_ids", None)
    if lister is not None:
        return list(await lister(limit))
    if isinstance(store, task_store.PostgresTaskStore):
        pool = await store._ready_pool()
        rows = await pool.fetch(_RECENT_IDS_SQL, limit, timeout=_LIST_TIMEOUT_SECONDS)
        return [str(row["task_id"]) for row in rows]
    return []


async def warm_recent_tasks(limit: int = WARM_TASKS, budget_seconds: float | None = None) -> int:
    """Load the newest tasks into `state`; returns how many are now held.
    Never raises. A no-op without a durable store."""
    store = task_store.get_task_store()
    if store is None:
        return 0
    loaded: list[str] = []

    async def warm() -> None:
        ids = await _recent_ids(store, limit)
        gate = asyncio.Semaphore(WARM_CONCURRENCY)

        async def one(task_id: str) -> None:
            async with gate:
                if await task_persistence.ensure_task(task_id):
                    loaded.append(task_id)

        await asyncio.gather(*(one(task_id) for task_id in ids))

    budget = WARM_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    try:
        await asyncio.wait_for(warm(), timeout=budget)
    except TimeoutError:
        logger.warning("task warm-up: stopped at its %.0f s budget with %d task(s) loaded", budget, len(loaded))
    except Exception as e:
        logger.warning(
            "task warm-up: the store could not be read (%s: %s) — %d task(s) loaded; the rest are read on demand",
            type(e).__name__,
            e,
            len(loaded),
        )
    else:
        logger.info("task warm-up: %d recent task(s) loaded from the store", len(loaded))
    return len(loaded)
