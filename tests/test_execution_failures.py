"""Failure paths of the execution service: a workflow that raises or is
cancelled must land in status="failed" and always close its trace stream, and
every per-step failure must reach the server log — trace lines alone are
invisible once the task is evicted."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state
from app.trace_bus import bus


def _plan(plan_id: str, agent_ids: tuple[str, ...] = ("agt_x",)) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent="exercise the failure path",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id=agent_id,
                    agent_name=f"w.{agent_id}",
                    rationale="r",
                    est_price_usdc=0.01,
                    est_eta_seconds=1.0,
                )
                for agent_id in agent_ids
            ]
        ),
        total_usdc=0.01 * len(agent_ids),
        total_eta=1.0,
    )


def _add_task(task_id: str) -> None:
    state.add_task(
        Task(
            id=task_id,
            intent="exercise the failure path",
            agents=1,
            spent=0.0,
            status="running",
            started="just now",
        )
    )


def _resolves_to(monkeypatch, lookup) -> None:
    """Pin the dispatch seam to `lookup`.

    execution_svc resolves a step's worker through the async `resolve_worker`
    (local registry first, then a stored binding). These tests only care which
    worker comes back, so a plain agent_id -> Worker|None lookup is adapted to
    that seam here instead of at every call site — including one that raises,
    which must still take down the whole run.
    """

    async def _resolve(agent_id):
        return lookup(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _drain(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _svc_errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.services.execution_svc" and r.levelno >= logging.ERROR]


class _BoomWorker:
    """Stands in for a dead provider: every call raises."""

    def __init__(self, name: str = "w.boom") -> None:
        self.name = name

    async def run(self, intent, rationale, context=None):
        raise RuntimeError("openai: invalid_api_key")


class _OkWorker:
    """Succeeds; optionally returns an artifact."""

    def __init__(self, name: str = "w.ok", *, artifact: bool = False) -> None:
        self.name = name
        self._artifact = artifact

    async def run(self, intent, rationale, context=None):
        output = {"summary": "did the thing"}
        if self._artifact:
            output["artifact"] = {
                "title": "Demo",
                "files": [{"path": "index.html", "language": "html", "content": "<p>hi</p>"}],
            }
        return output


def test_mid_run_exception_marks_task_failed(monkeypatch):
    def boom(agent_id):
        raise RuntimeError("registry exploded")

    _resolves_to(monkeypatch, boom)
    task_id = "tsk_fail_exc"
    _add_task(task_id)

    async def run():
        q = bus.subscribe(task_id)
        await execution_svc._run(_plan("pln_fail_exc"), task_id)
        return q

    q = asyncio.run(run())
    assert state.tasks[task_id].status == "failed"
    lines = _drain(q)
    assert lines[-1] is None  # SSE terminator: bus.close ran
    errors = [ln for ln in lines if ln is not None and ln.level == "error"]
    assert any("workflow failed: registry exploded" in ln.msg for ln in errors)
    assert task_id not in bus._subs


def test_step_exception_is_logged_with_context_and_traceback(monkeypatch, caplog):
    _resolves_to(monkeypatch, lambda agent_id: _BoomWorker())
    task_id = "tsk_step_log_exc"
    _add_task(task_id)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        asyncio.run(execution_svc._run(_plan("pln_step_log_exc"), task_id))

    records = _svc_errors(caplog)
    assert records, "a swallowed step failure was never logged"
    msg = records[0].getMessage()
    # Diagnosable without a debugger: which task, which agent, which worker,
    # what the provider said — plus the traceback.
    assert task_id in msg
    assert "agt_x" in msg
    assert "w.boom" in msg
    assert "openai: invalid_api_key" in msg
    assert records[0].exc_info is not None


def test_step_timeout_is_logged(monkeypatch, caplog):
    class HangingWorker:
        name = "w.hang"

        async def run(self, intent, rationale, context=None):
            await asyncio.sleep(30)

    _resolves_to(monkeypatch, lambda agent_id: HangingWorker())
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 0.05)
    task_id = "tsk_step_log_timeout"
    _add_task(task_id)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        asyncio.run(execution_svc._run(_plan("pln_step_log_timeout"), task_id))

    records = _svc_errors(caplog)
    assert records, "a swallowed step timeout was never logged"
    msg = records[0].getMessage()
    assert task_id in msg
    assert "w.hang" in msg
    assert "timed out" in msg


def test_unknown_agent_step_is_logged(monkeypatch, caplog):
    _resolves_to(monkeypatch, lambda agent_id: None)
    task_id = "tsk_step_log_unknown"
    _add_task(task_id)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        asyncio.run(execution_svc._run(_plan("pln_step_log_unknown"), task_id))

    records = _svc_errors(caplog)
    assert records, "an unknown agent was never logged"
    msg = records[0].getMessage()
    assert task_id in msg
    assert "agt_x" in msg
    assert "unknown agent" in msg


# ── terminal status of a run whose steps failed ─────────────────
def test_run_with_every_step_failing_is_not_complete(monkeypatch):
    """The total-outage case: every step raises, the loop continues past all of
    them, and the run must NOT be finalized as "complete"."""
    _resolves_to(monkeypatch, lambda agent_id: _BoomWorker())
    task_id = "tsk_all_steps_failed"
    _add_task(task_id)

    async def run():
        q = bus.subscribe(task_id)
        await execution_svc._run(_plan("pln_all_steps_failed", ("agt_x", "agt_y")), task_id)
        return q

    q = asyncio.run(run())
    task = state.tasks[task_id]
    assert task.status == "failed"
    assert task.status != "complete"
    assert task.artifact is None
    assert task.spent == 0.0

    lines = [ln for ln in _drain(q) if ln is not None]
    assert any("workflow incomplete — 0/2 agents produced output" in ln.msg for ln in lines)
    # Nothing was produced, so nothing is attested to.
    assert not any("sealed" in ln.msg for ln in lines)


def test_run_with_every_step_failing_is_logged(monkeypatch, caplog):
    _resolves_to(monkeypatch, lambda agent_id: _BoomWorker())
    task_id = "tsk_all_steps_failed_log"
    _add_task(task_id)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        asyncio.run(execution_svc._run(_plan("pln_all_steps_failed_log"), task_id))

    messages = [r.getMessage() for r in _svc_errors(caplog)]
    assert any(f"task {task_id} finalized as failed" in m and "0/1 steps produced output" in m for m in messages)


def test_partial_run_that_still_produced_an_artifact_is_complete(monkeypatch):
    """Degraded but delivered: the caller still receives the artifact."""
    workers = {"agt_x": _OkWorker("w.gen", artifact=True), "agt_y": _BoomWorker("w.critic")}
    _resolves_to(monkeypatch, workers.get)
    task_id = "tsk_partial_with_artifact"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan("pln_partial_with_artifact", ("agt_x", "agt_y")), task_id))
    task = state.tasks[task_id]
    assert task.status == "complete"
    assert task.artifact is not None
    assert task.spent == 0.01  # only the step that ran was billed


def test_partial_run_without_an_artifact_is_failed(monkeypatch):
    """Half a workflow with nothing to hand back is not a success."""
    workers = {"agt_x": _OkWorker("w.research"), "agt_y": _BoomWorker("w.gen")}
    _resolves_to(monkeypatch, workers.get)
    task_id = "tsk_partial_no_artifact"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan("pln_partial_no_artifact", ("agt_x", "agt_y")), task_id))
    task = state.tasks[task_id]
    assert task.status == "failed"
    assert task.artifact is None


def test_fully_successful_run_is_complete(monkeypatch):
    workers = {"agt_x": _OkWorker("w.a", artifact=True), "agt_y": _OkWorker("w.b")}
    _resolves_to(monkeypatch, workers.get)
    task_id = "tsk_all_steps_ok"
    _add_task(task_id)

    async def run():
        q = bus.subscribe(task_id)
        await execution_svc._run(_plan("pln_all_steps_ok", ("agt_x", "agt_y")), task_id)
        return q

    q = asyncio.run(run())
    assert state.tasks[task_id].status == "complete"
    lines = [ln for ln in _drain(q) if ln is not None]
    assert any("workflow sealed" in ln.msg for ln in lines)
    assert not any("workflow incomplete" in ln.msg for ln in lines)


def test_unknown_agent_counts_as_a_failed_step(monkeypatch):
    _resolves_to(monkeypatch, lambda agent_id: None)
    task_id = "tsk_unknown_agent_status"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan("pln_unknown_agent_status"), task_id))
    assert state.tasks[task_id].status == "failed"


def test_cancelled_run_marks_task_failed_and_closes_bus(monkeypatch):
    class SlowWorker:
        name = "w.slow"

        async def run(self, intent, rationale, context=None):
            await asyncio.sleep(30)

    _resolves_to(monkeypatch, lambda agent_id: SlowWorker())
    task_id = "tsk_fail_cancel"
    _add_task(task_id)

    async def run():
        q = bus.subscribe(task_id)
        t = asyncio.create_task(execution_svc._run(_plan("pln_fail_cancel"), task_id))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return q

    q = asyncio.run(run())
    assert state.tasks[task_id].status == "failed"
    lines = _drain(q)
    assert lines[-1] is None  # cleanup survived the cancellation
    errors = [ln for ln in lines if ln is not None and ln.level == "error"]
    assert any("workflow cancelled" in ln.msg for ln in errors)
    assert task_id not in bus._subs
