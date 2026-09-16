"""Every way a step can fail reaches the failure tracker (story 2.03).

`record_failure` was called from exactly ONE of the run loop's four step-failure
paths — the generic `except Exception`. The outer deadline, a worker returning
something that is not a dict, and the unusable-field gate all failed the step,
logged it and moved on without touching the counter. So an agent could fail
every step of every run and the module built to answer "is this agent broken?"
would report a streak of zero.

Harmless for a bound external agent today — its own deadline fires before the
loop's, and the response contract guarantees the output shape — and that is
exactly why it needed pinning: the three uncounted paths are the ones a LOCAL
worker reaches, and the gap is one worker change away from mattering.

The tracker is exercised for real, not just spied on: the class each path
reports has to survive `_normalize_rule`, which collapses anything that is not
a lowercase snake_case token to "unclassified" — a counted failure carrying a
useless class would be a counter with the diagnosis removed.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.services import failure_tracker as ft
from app.state import state

INTENT = "ship the launch page"
PRICE = 0.02
LOGGER_NAME = "app.services.failure_tracker"


@pytest.fixture(autouse=True)
def clean():
    """Streaks and the at-capacity flag are process-global, and the coalescing
    guards are exactly the state a leaked streak would disarm."""
    ft._streaks.clear()
    ft._at_capacity = False
    yield
    ft._streaks.clear()
    ft._at_capacity = False
    for tid in [t for t in state.tasks if t.startswith("tsk_count_")]:
        state.tasks.pop(tid, None)
        state.traces.pop(tid, None)


class _Worker:
    """A worker that fails the way each test needs it to."""

    real = True

    def __init__(self, name: str, output: object = None, *, hangs: bool = False) -> None:
        self.name = name
        self._output = output
        self._hangs = hangs

    async def run(self, intent, rationale, context=None):
        if self._hangs:
            await asyncio.sleep(5)
        return self._output


def _run_one(monkeypatch, task_id: str, worker: _Worker) -> None:
    async def _resolve(agent_id: str):
        return worker if agent_id == "agt_x" else None

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)
    plan = StoredPlan(
        id="pln_" + task_id,
        intent=INTENT,
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id="agt_x", agent_name="agt_x", rationale="do it", est_price_usdc=PRICE, est_eta_seconds=1.0
                )
            ]
        ),
        total_usdc=PRICE,
        total_eta=1.0,
    )
    state.add_task(Task(id=task_id, intent=INTENT, agents=1, spent=0.0, status="running"))
    asyncio.run(execution_svc._run(plan, task_id))


def _first_failure_line(caplog) -> str:
    """The tracker's opening WARNING, which names the class it stored."""
    return next(r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and "failed a step" in r.getMessage())


def test_a_step_that_timed_out_is_counted(monkeypatch, caplog):
    """The outer `asyncio.wait_for` ceiling. An external worker's own deadline
    fires first, so what reaches here is a LOCAL worker hanging — the one
    failure mode the streak could not see at all."""
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _run_one(monkeypatch, "tsk_count_timeout", _Worker("w.slow", hangs=True))

    assert ft.consecutive_failures("agt_x") == 1
    assert "step_timeout" in _first_failure_line(caplog)


def test_a_non_dict_return_is_counted(monkeypatch, caplog):
    """A worker handing back something that is not a dict fails its step like
    a raised exception, and now counts like one."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _run_one(monkeypatch, "tsk_count_nondict", _Worker("w.str", "not a dict"))

    assert ft.consecutive_failures("agt_x") == 1
    assert "not_a_dict" in _first_failure_line(caplog)


def test_an_unusable_output_field_is_counted(monkeypatch, caplog):
    """The `_unusable_field` gate. `artifact` is a string here, which the
    post-step handling cannot walk — that step failed, unbilled, and the agent
    that produced it is failing."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _run_one(monkeypatch, "tsk_count_unusable", _Worker("w.bad", {"summary": "ok", "artifact": "boom"}))

    assert ft.consecutive_failures("agt_x") == 1
    assert "unusable_output" in _first_failure_line(caplog)


def test_every_new_class_is_a_token_the_tracker_keeps():
    """The three classes are validated by SHAPE, not membership — the run loop
    must not import a worker module to classify a failure. A token that failed
    that shape would be stored as "unclassified", counting the failure while
    throwing away which failure it was."""
    for rule in (
        execution_svc.STEP_TIMEOUT_FAILURE,
        execution_svc.NOT_A_DICT_FAILURE,
        execution_svc.UNUSABLE_OUTPUT_FAILURE,
    ):
        assert ft._normalize_rule(rule) == rule


def test_a_counted_failure_still_clears_on_the_next_success(monkeypatch):
    """A streak is evidence of a CURRENT outage, so the new call sites must
    feed the same counter the success path clears — not a parallel one that
    keeps an agent marked broken after it recovers."""
    _run_one(monkeypatch, "tsk_count_recovers", _Worker("w.str", "not a dict"))
    assert ft.consecutive_failures("agt_x") == 1

    _run_one(monkeypatch, "tsk_count_recovered", _Worker("w.ok", {"summary": "delivered"}))
    assert ft.consecutive_failures("agt_x") == 0
