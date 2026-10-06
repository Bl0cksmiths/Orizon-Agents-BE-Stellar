"""LazyAgent: an agno Agent built on first use, indistinguishable once built."""

from __future__ import annotations

import asyncio
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
