"""The decompose fan-out is bounded too: the free-form path's planning LLM
call runs under the decompose_max_concurrent semaphore, while the demo-kit
short circuit (no LLM call) never takes the gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config import settings
from app.schemas import Plan
from app.seed import seed_registry
from app.services import orchestrator_svc


def test_free_form_decompose_is_bounded(monkeypatch):
    seed_registry()
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)

    async def scenario() -> None:
        release = asyncio.Event()
        in_call = asyncio.Event()
        calls = 0

        async def fake_arun(prompt):
            nonlocal calls
            calls += 1
            in_call.set()
            await release.wait()
            return SimpleNamespace(content=Plan(steps=[]))

        monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", fake_arun)

        first = asyncio.create_task(orchestrator_svc.decompose("write a haiku about databases"))
        await asyncio.wait_for(in_call.wait(), timeout=5)
        second = asyncio.create_task(orchestrator_svc.decompose("draft a landing page for a bakery"))
        await asyncio.sleep(0.1)
        # The second planning call queued behind the gate: one LLM call in
        # flight at a time, never a fan-out.
        assert calls == 1
        release.set()
        await asyncio.gather(first, second)
        assert calls == 2

    asyncio.run(scenario())


def test_kit_decompose_never_takes_the_gate(monkeypatch):
    seed_registry()
    monkeypatch.setattr(settings, "decompose_max_concurrent", 1)

    # Recorded rather than raised: decompose degrades a planner call that
    # raises to its fallback plan (BLO-121), which would swallow the raise.
    llm_calls = []

    async def record_arun(prompt):
        llm_calls.append(prompt)
        return None

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", record_arun)

    async def scenario() -> None:
        gate = orchestrator_svc._decompose_gate()
        await gate.acquire()  # saturate the gate — a free-form call would queue
        try:
            plan = await asyncio.wait_for(
                orchestrator_svc.decompose("pomodoro timer app"),
                timeout=10,
            )
        finally:
            gate.release()
        assert len(plan.steps) >= 4

    asyncio.run(scenario())
