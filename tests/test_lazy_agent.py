"""LazyAgent: an agno Agent built on first use, indistinguishable once built."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.agents import model_factory
from app.agents.model_factory import LazyAgent, lazy_agent


class _Built:
    instructions = "be brief"

    async def arun(self, prompt: str, **kwargs: Any) -> str:
        return f"ran {prompt} {kwargs}"


def test_nothing_is_built_until_first_use_and_then_only_once() -> None:
    builds: list[int] = []

    def build() -> _Built:
        builds.append(1)
        return _Built()

    agent = LazyAgent(build)
    assert builds == [] and agent.built is False
    assert agent.instructions == "be brief"
    assert asyncio.run(agent.arun("x", stream=False)) == "ran x {'stream': False}"
    assert builds == [1] and agent.built is True


def test_arun_can_be_replaced_on_the_wrapper_like_on_an_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = LazyAgent(_Built)

    async def fake(prompt: str) -> str:
        return "fake"

    monkeypatch.setattr(agent, "arun", fake)
    assert asyncio.run(agent.arun("x")) == "fake"
    monkeypatch.undo()
    assert asyncio.run(agent.arun("x")) == "ran x {}"


def test_private_names_never_trigger_a_build() -> None:
    agent = LazyAgent(lambda: (_ for _ in ()).throw(AssertionError("built")))
    with pytest.raises(AttributeError):
        _ = agent._private
    assert agent.built is False


def test_lazy_agent_builds_a_real_agno_agent_with_the_shared_model_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from agno.agent import Agent

    monkeypatch.setattr(model_factory.settings, "llm_timeout_seconds", 17.0)
    agent = lazy_agent(name="probe", model_id="gpt-test", instructions="hi")
    built = agent.agent
    assert isinstance(built, Agent)
    assert (built.name, built.instructions) == ("probe", "hi")
    assert (built.model.id, built.model.timeout, built.model.max_retries) == ("gpt-test", 17.0, 1)


_PROBE = (
    "import sys, app.main; print(sorted(m for m in ('agno.agent', 'agno.models.openai', 'openai') if m in sys.modules))"
)


def test_importing_the_app_imports_neither_agno_nor_openai() -> None:
    """The point of building agents lazily: ~0.5 s of agno and openai imports
    stay off the boot path. A module-level agno import anywhere in the app
    would put it back, and this is where that shows."""
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "OPENAI_API_KEY": "sk-test"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip().splitlines()[-1] == "[]"


def test_the_planner_and_every_llm_worker_hold_a_lazy_agent() -> None:
    from app.agents.orchestrator import orchestrator_agent
    from app.agents.registry import WORKERS

    def agent_of(worker: object) -> object:
        critic = getattr(worker, "_critic", None)
        return getattr(critic if critic is not None else worker, "_agent", None)

    held = [a for a in (agent_of(w) for w in WORKERS.values()) if a is not None]
    assert len(held) == 7  # code.gen, code.critic, copywrite, seo, research, design tokens, sol audit
    assert all(isinstance(agent, LazyAgent) for agent in [orchestrator_agent, *held])
