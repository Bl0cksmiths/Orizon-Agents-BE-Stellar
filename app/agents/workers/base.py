from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from ...config import settings
from ..model_factory import claude_workers, served_model_label, step_model_label, worker_tier

if TYPE_CHECKING:
    from ...llm.tiers import Tier


class Worker(ABC):
    id: str
    name: str
    real: bool

    @abstractmethod
    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute the agent's task; return a small dict describing the output.

        `context` carries forward results from prior pipeline steps and (when
        present) the curated `DemoKit` for the intent. Keys of interest:
          - context['kit']           → DemoKit dict (or None)
          - context['research.pro']  → prior research brief
          - context['seo.brief']     → prior brand block
          - context['design.figma']  → prior design tokens
          - context['code.gen']      → prior code artifact
        Workers are free to ignore the kwarg if they don't need context.
        """
        raise NotImplementedError


class ModelWorker(Worker):
    """A built-in worker whose free-form path asks a language model.

    The run loop hands it the plan step's `tier` (`run(..., tier=)`), which
    picks the Claude model the step runs on. `default_tier` is what a step with
    no tier runs on — chosen per worker for what its output needs, not one
    global default:

      copywrite.v3, seo.brief, design.figma  low       short, well-shaped JSON
      research.pro, code.gen, code.critic    moderate  synthesis / a full app
      sol-audit                              complex   security reasoning

    A worker with a deterministic path (a curated kit, a baked artifact) says
    so in `_deterministic`, which both `run` and `step_model` read — so the
    trace never names a model for a step that did not ask one.
    """

    default_tier: ClassVar[Tier]

    def _deterministic(self, context: dict[str, Any] | None) -> bool:
        """True when this step will be served without asking a model."""
        return False

    def step_model(self, tier: object, context: dict[str, Any] | None) -> str | None:
        """The trace's description of the model this step will run on, or None
        when the step takes a deterministic path and asks no model at all."""
        if self._deterministic(context):
            return None
        if not claude_workers():
            return settings.worker_model
        return step_model_label(worker_tier(tier, self.default_tier))

    def served_model(self, tier: object, model: str) -> str:
        """The trace's description of a fallback `model` that served this step."""
        return served_model_label(worker_tier(tier, self.default_tier), model)

    @abstractmethod
    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
