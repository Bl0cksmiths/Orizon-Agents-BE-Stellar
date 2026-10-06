"""The base for a built-in worker that runs on Claude and nothing else.

code.next, vision.ocr, ads.meta and translate.42 were simulated until they got
real workers, and they got them on Claude only: there is no agno + OpenAI path
behind them to fall back to. So while `ORCHESTRATOR_PROVIDER` is "openai"
such a step must not produce anything — no stand-in text a buyer could be
charged for — and fails as `model_not_configured` instead, which the run loop
treats as our outage: not run, not charged, not rated.

The trace names no model for a step that will not ask one, so `step_model`
says None off Claude rather than naming the OpenAI worker model — and, for
the same reason, `upstream_sources` names no earlier step whose output it used.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from ..model_factory import claude_workers
from . import claude_step
from .base import ModelWorker

if TYPE_CHECKING:
    from ...llm.tiers import Tier


class ClaudeOnlyWorker(ModelWorker):
    """A `ModelWorker` with no OpenAI path: off Claude, every step is not attempted."""

    def step_model(self, tier: object, context: dict[str, Any] | None) -> str | None:
        if not claude_workers():
            return None
        return super().step_model(tier, context)

    def upstream_sources(self, context: dict[str, Any] | None) -> list[str]:
        # Off Claude no prompt is built, so no earlier output is used.
        if not claude_workers():
            return []
        return super().upstream_sources(context)

    def require_claude(self) -> None:
        """Raise `model_not_configured` unless the workers run on Claude."""
        if not claude_workers():
            raise claude_step.ModelStepError(
                claude_step.MODEL_NOT_CONFIGURED, f"{self.name}: runs on Claude only, and the provider is not Claude"
            )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
    ) -> dict[str, Any]:
        self.require_claude()
        return await self.run_on_claude(intent, rationale, context or {}, self.effective_tier(tier))

    @abstractmethod
    async def run_on_claude(self, intent: str, rationale: str, context: dict[str, Any], tier: Tier) -> dict[str, Any]:
        """The step on Claude, at the tier it runs on (default and cap applied)."""
        raise NotImplementedError
