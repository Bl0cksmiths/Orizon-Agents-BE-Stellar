from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from ..schemas import TraceLine
from ..state import state
from ..task_auth import require_task_read
from ..trace_bus import bus

router = APIRouter(tags=["trace"])


@router.get(
    "/trace/{task_id}",
    response_model=list[TraceLine],
    summary="Get a task's recorded trace",
    dependencies=[Depends(require_task_read)],
)
async def get_trace(task_id: str) -> list[TraceLine]:
    if task_id not in state.traces and task_id not in state.tasks:
        # A bare snake token, never the id — `task_auth.require_task_read`'s
        # rule, and this is the route that most needs it: these two are the
        # world-readable ones while TASK_AUTH_REQUIRED is off, which is the
        # shipped default. `main.http_exception_handler` promotes a snake
        # detail to `error.code` and derives the message from it; an
        # interpolated id is not one, so it fell through to `not_found` and
        # the handler copied the caller's own text into `error.message`.
        # `task_id` carries no length bound here either.
        raise HTTPException(404, "unknown_task")
    return state.traces.get(task_id, [])


def _replay_events(task_id: str) -> Iterator[dict[str, str]]:
    for line in state.traces.get(task_id, []):
        yield {"event": "trace", "data": line.model_dump_json()}


@router.get(
    "/trace/{task_id}/stream",
    summary="Stream a task's trace as SSE",
    dependencies=[Depends(require_task_read)],
)
async def stream_trace(task_id: str) -> EventSourceResponse:
    """Server-Sent Events — replays the existing trace then streams live lines.

    The task-auth guard accepts the read token as a `?token=` query parameter
    here because EventSource cannot set request headers.

    Client disconnects are handled by sse-starlette cancelling the generator
    (the finally block unsubscribes); polling request.is_disconnected() here
    would race sse-starlette's own listener for the one ASGI receive channel.
    """
    if task_id not in state.traces and task_id not in state.tasks:
        raise HTTPException(404, "unknown_task")  # `get_trace`'s rule

    task = state.tasks.get(task_id)
    running = task is not None and task.status in ("pending", "running") and not bus.is_closed(task_id)

    if not running:
        # Finished (or never started) — replay history and end the stream.
        # No subscription: no producer exists, so a live queue would only
        # ping forever and leak.
        async def replay() -> AsyncIterator[dict[str, str]]:
            for event in _replay_events(task_id):
                yield event
            yield {"event": "done", "data": "{}"}

        return EventSourceResponse(replay())

    queue = bus.subscribe(task_id)

    async def generator() -> AsyncIterator[dict[str, str]]:
        try:
            # Replay anything already recorded so late subscribers see full history.
            for event in _replay_events(task_id):
                yield event

            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # keepalive
                    yield {"event": "ping", "data": "{}"}
                    continue

                if line is None:
                    yield {"event": "done", "data": "{}"}
                    break
                yield {"event": "trace", "data": line.model_dump_json()}
        finally:
            bus.unsubscribe(task_id, queue)

    return EventSourceResponse(generator())
