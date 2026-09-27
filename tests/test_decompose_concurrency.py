"""The decompose fan-out is bounded too: the free-form path's planning LLM
call runs under the decompose_max_concurrent gate, a bounded number of calls
may wait for a slot and the rest are refused, while the demo-kit short circuit
(no LLM call) never takes the gate."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace

import pytest

from app.config import settings
from app.schemas import Plan
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.state import state

FREE_FORM = "write a haiku about databases"


async def _no_sleep(*_a: object, **_k: object) -> None:
    return None


@pytest.fixture(autouse=True)
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh seeded registry and a fresh gate, both restored afterwards."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(orchestrator_svc, "_plan_gate", None)
    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_sleep, raising=False)
    yield
    state.agents.clear()
    state.agents.update(saved)


async def _until(condition: Callable[[], bool], *, turns: int = 1000) -> None:
    """Yield to the loop until `condition` holds — a positive signal, not a nap."""
    for _ in range(turns):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def _held_planner(release: asyncio.Event, calls: list[str]) -> Callable[[str], Awaitable[SimpleNamespace]]:
    async def _arun(prompt: str) -> SimpleNamespace:
        calls.append(prompt)
        await release.wait()
        return SimpleNamespace(content=Plan(steps=[]))

    return _arun


def test_free_form_decompose_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)

    async def scenario() -> None:
        release = asyncio.Event()
        calls: list[str] = []
        monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _held_planner(release, calls))
        gate = orchestrator_svc._decompose_gate()

        first = asyncio.create_task(orchestrator_svc.decompose(FREE_FORM))
        await _until(lambda: len(calls) == 1)
        second = asyncio.create_task(orchestrator_svc.decompose("draft a landing page for a bakery"))
        # Positively queued behind the gate — not merely slow to arrive.
        await _until(lambda: gate.waiting == 1)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert len(calls) == 2
        assert (gate.in_flight, gate.waiting) == (0, 0)

    asyncio.run(scenario())


def test_a_full_wait_queue_refuses_instead_of_queueing(monkeypatch: pytest.MonkeyPatch) -> None:
    # One running, one waiting, and the queue holds one: the third request is
    # refused at once and never reaches the planner. Waiters used to queue
    # without limit, each holding a connection for the whole budget.
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)
    monkeypatch.setattr(settings, "decompose_max_queued", 1)

    async def scenario() -> None:
        release = asyncio.Event()
        calls: list[str] = []
        monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _held_planner(release, calls))
        gate = orchestrator_svc._decompose_gate()

        running = asyncio.create_task(orchestrator_svc.decompose(FREE_FORM))
        await _until(lambda: len(calls) == 1)
        queued = asyncio.create_task(orchestrator_svc.decompose(FREE_FORM))
        await _until(lambda: gate.waiting == 1)

        with pytest.raises(orchestrator_svc.PlannerBusyError):
            await orchestrator_svc.decompose(FREE_FORM)
        assert len(calls) == 1

        release.set()
        await asyncio.gather(running, queued)
        assert len(calls) == 2

    asyncio.run(scenario())


async def _hold(gate: orchestrator_svc._PlanGate, release: asyncio.Event, entered: list[int]) -> None:
    """Take a slot (queueing if need be), note it, and keep it until released."""
    async with gate.slot(1):
        entered.append(1)
        await release.wait()


def test_a_waiter_that_times_out_gives_its_place_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # A queued request whose budget runs out leaves the queue, so a queue of
    # one is open again for the next request rather than full forever.
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)

    async def scenario() -> None:
        gate = orchestrator_svc._decompose_gate()
        release = asyncio.Event()
        entered: list[int] = []
        holder = asyncio.create_task(_hold(gate, release, entered))
        await _until(lambda: entered == [1])

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_hold(gate, release, entered), timeout=0.01)
        assert gate.waiting == 0

        late = asyncio.create_task(_hold(gate, release, entered))
        await _until(lambda: gate.waiting == 1)
        release.set()
        await asyncio.gather(holder, late)
        assert entered == [1, 1]
        assert (gate.in_flight, gate.waiting) == (0, 0)

    asyncio.run(scenario())


def test_the_gate_survives_a_second_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # An asyncio.Semaphore binds to the first loop a waiter contends on and
    # raises in the next one; the gate is module-global, so every test that
    # planned under contention in its own asyncio.run inherited that trap.
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)

    async def contend() -> None:
        gate = orchestrator_svc._decompose_gate()
        release = asyncio.Event()
        entered: list[int] = []
        first = asyncio.create_task(_hold(gate, release, entered))
        await _until(lambda: entered == [1])
        second = asyncio.create_task(_hold(gate, release, entered))
        await _until(lambda: gate.waiting == 1)
        release.set()
        await asyncio.gather(first, second)
        assert entered == [1, 1]

    asyncio.run(contend())
    asyncio.run(contend())


def test_kit_decompose_never_takes_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)
    monkeypatch.setattr(settings, "decompose_max_queued", 0)

    # Recorded rather than raised: decompose degrades a planner call that
    # raises to its fallback plan (BLO-121), which would swallow the raise.
    llm_calls = []

    async def record_arun(prompt):
        llm_calls.append(prompt)
        return None

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", record_arun)

    async def scenario() -> None:
        # Saturated, with no room to queue: a free-form call would be refused.
        async with orchestrator_svc._decompose_gate().slot(0):
            plan = await asyncio.wait_for(orchestrator_svc.decompose("pomodoro timer app"), timeout=10)
        assert len(plan.steps) >= 4

    asyncio.run(scenario())
    assert llm_calls == [], "kit decompose must never reach the LLM"
