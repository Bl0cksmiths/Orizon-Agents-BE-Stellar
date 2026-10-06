"""Shared model factory — every worker's model is chosen here.

Two providers sit behind `ORCHESTRATOR_PROVIDER` (`app/llm/provider.py`):
Claude, tier-routed through `app/llm/claude.py`, and the older agno +
OpenAI path below, kept until Claude is proven live. `claude_workers()` is
the one switch every built-in LLM worker reads, per call, so flipping the
provider needs no restart and no worker knows how it is decided.

On Claude a step runs on its plan tier's model (`app/llm/tiers.py`): low →
Claude Haiku 4.5, moderate → Claude Sonnet 5.5, complex → Claude Opus 5.5.
A step with no tier — a plan stored before steps carried one — runs on the
worker's own `default_tier` (see `ModelWorker` in `workers/base.py`).

The OpenAI path:

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
from ..llm.tiers import TIERS, Tier, display_name, model_for

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


# ── Claude ────────────────────────────────────────────────────────────────


def claude_workers() -> bool:
    """True when the built-in workers run on Claude rather than agno + OpenAI."""
    from ..llm.provider import active_provider

    return active_provider() == "anthropic"


def worker_tier(tier: object, default: Tier) -> Tier:
    """The tier a step runs on: its own when it names a known one, else `default`.

    `tier` is read off a stored plan, so it is checked rather than trusted — an
    unknown value runs on the worker's default instead of failing the model
    lookup mid-run.
    """
    for known in TIERS:
        if tier == known:
            return known
    return default


def step_model_label(tier: Tier) -> str:
    """The trace's name for the model a step of `tier` runs on, with the tier."""
    return f"{display_name(model_for(tier))} (tier: {tier})"
