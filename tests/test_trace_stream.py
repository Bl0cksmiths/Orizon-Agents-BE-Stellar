"""SSE trace streams must terminate: a finished task replays history and ends
with `done` instead of pinging forever, and the bus never leaks subscriber
queues or empty _subs keys."""

from __future__ import annotations

import asyncio
import time

from app.schemas import Task, TraceLine
from app.state import state
from app.trace_bus import bus


def _make_task(task_id: str, status: str) -> None:
    state.add_task(
        Task(
            id=task_id,
            intent="test intent",
            agents=1,
            spent=0.01,
            status=status,  # type: ignore[arg-type]
            started="now",
        )
    )


def _collect_events(client, task_id: str) -> list[str]:
    """Read the SSE stream to completion; return the `event:` lines."""
    events: list[str] = []
    with client.stream("GET", f"/api/trace/{task_id}/stream") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if line.startswith("event: done") or len(events) > 500:
                break
    return events


def test_stream_of_completed_task_replays_and_terminates(client):
    task_id = "tsk_stream_done"
    _make_task(task_id, "complete")
    state.append_trace(task_id, TraceLine(t="0.01", level="exec", msg="step one"))
    state.append_trace(task_id, TraceLine(t="0.02", level="out", msg="finished"))

    start = time.monotonic()
    events = _collect_events(client, task_id)

    # Terminates promptly (no 15 s keepalive loop) with history + done.
    assert time.monotonic() - start < 5.0
    assert events == ["trace", "trace", "done"]
    assert task_id not in bus._subs


def test_subscriber_during_run_gets_history_and_done(client):
    plan = client.post("/api/orchestrator/decompose", json={"intent": "pomodoro timer app"}).json()
    task_id = client.post("/api/orchestrator/execute", json={"plan_id": plan["plan_id"]}).json()["task_id"]

    # Subscribe mid-run (or just after — the finished path also ends in done).
    events = _collect_events(client, task_id)

    assert events, "stream produced no events"
    assert events[-1] == "done"
    assert "trace" in events
    assert task_id not in bus._subs


def test_bus_subscribe_after_close_gets_immediate_sentinel():
    async def scenario() -> None:
        task_id = "tsk_bus_closed"
        line = TraceLine(t="0.01", level="exec", msg="live line")

        q1 = bus.subscribe(task_id)
        await bus.publish(task_id, line)
        await bus.close(task_id)
        assert await q1.get() == line
        assert await q1.get() is None  # sentinel from close()

        # Late subscriber on a finished task: pre-loaded sentinel, no _subs key.
        q2 = bus.subscribe(task_id)
        assert q2.get_nowait() is None
        assert task_id not in bus._subs

    asyncio.run(scenario())


def test_bus_unsubscribe_removes_empty_key():
    task_id = "tsk_bus_leak"
    q = bus.subscribe(task_id)
    assert task_id in bus._subs
    bus.unsubscribe(task_id, q)
    assert task_id not in bus._subs


# `tests/test_tasks_api.py` carries the full reasoning. These two matter most
# of the four: with TASK_AUTH_REQUIRED off — the shipped default, and how
# production runs — they are world-readable, so the caller whose text was
# being reflected needed no credential at all. No slash in it, or the router
# answers its own 404 before the handler runs.
HOSTILE_TASK_ID = "A" * 4096 + "<script>alert(1)"


def test_an_unknown_trace_is_refused_without_echoing_the_id(client):
    r = client.get(f"/api/trace/{HOSTILE_TASK_ID}")

    assert r.status_code == 404
    assert r.json()["error"] == {
        "code": "unknown_task",
        "message": "unknown task",
        "request_id": r.headers["x-request-id"],
    }
    assert HOSTILE_TASK_ID not in r.text


def test_an_unknown_trace_stream_is_refused_without_echoing_the_id(client):
    # The SSE route refuses before any stream is opened, so the 404 is an
    # ordinary JSON envelope and the same rule applies to it.
    r = client.get(f"/api/trace/{HOSTILE_TASK_ID}/stream")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_task"
    assert HOSTILE_TASK_ID not in r.text
