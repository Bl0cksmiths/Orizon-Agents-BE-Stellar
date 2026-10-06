"""Shared OpenAIChat factory — every Agno agent's model is built here.

Centralizes the client budget: `timeout` caps a single OpenAI HTTP attempt
(the SDK default is 600 s) and `max_retries=1` caps the SDK's internal retry
loop (default 2 extra attempts), so a hung upstream can't pin one request
for ~30 minutes. End-to-end bounds (e.g. decompose_timeout_seconds) are
enforced with asyncio.wait_for at the call sites.

And every agno Agent is built LAZILY, on its first use (`lazy_agent`). agno and
the openai SDK cost about half a second to import, and the planner and the
workers are module-level objects, so building them at import put that half
second on every boot — every wake from idle on the free tier — for a request
that is almost always a page read and never touches a model. The first plan
or run pays it instead, once per process.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..config import settings

if TYPE_CHECKING:
    from agno.models.openai import OpenAIChat


def build_openai_chat(model_id: str) -> OpenAIChat:
    """OpenAIChat with the shared api key, request timeout, and retry cap."""
    from agno.models.openai import OpenAIChat

    return OpenAIChat(
        id=model_id,
        api_key=settings.openai_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
    )


class LazyAgent:
    """An agno Agent built the first time it is used.

    `arun` and every other attribute reach the built agent, so a caller — or a
    test replacing `arun` on it — sees an Agent. Built at most once.
    """

    def __init__(self, build: Callable[[], Any]) -> None:
        self._build = build
        self._agent: Any = None

    @property
    def agent(self) -> Any:
        """The agno Agent, built now if it has not been yet."""
        if self._agent is None:
            self._agent = self._build()
        return self._agent

    @property
    def built(self) -> bool:
        return self._agent is not None

    async def arun(self, *args: Any, **kwargs: Any) -> Any:
        return await self.agent.arun(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names this wrapper does not define. Private names are
        # refused rather than built for, so copy/pickle probing cannot trigger
        # a build or recurse before __init__ has run.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.agent, name)


def lazy_agent(*, model_id: str, **agent_kwargs: Any) -> LazyAgent:
    """An agno `Agent(model=<OpenAIChat model_id>, **agent_kwargs)`, built on first use."""

    def build() -> Any:
        from agno.agent import Agent

        return Agent(model=build_openai_chat(model_id), **agent_kwargs)

    return LazyAgent(build)
