from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import claude_workers, lazy_agent, worker_tier
from . import claude_step
from .base import ModelWorker
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier


class Section(BaseModel):
    title: str
    body: str = Field(..., max_length=280)


class CopyOutput(BaseModel):
    hero_headline: str
    hero_subtitle: str
    sections: list[Section] = Field(..., min_length=2, max_length=5)


INSTRUCTIONS = (
    "You are a senior marketing copywriter. Given an intent, draft a hero "
    "headline (<=80 chars), a hero subtitle (<=160 chars), and 3–4 landing "
    "sections with a title and short body each. Punchy, concrete, outcome-focused."
)

# Room for the JSON plus any thinking the tier's model does first.
MAX_TOKENS = 8_000


class Copywrite(ModelWorker):
    id = "agt_01h8"
    name = "copywrite.v3"
    real = True
    default_tier = "low"

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="copywrite.v3",
            model_id=settings.worker_model,
            instructions=INSTRUCTIONS,
            output_schema=CopyOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
    ) -> dict[str, Any]:
        prompt = worker_prompt(intent, rationale, "Draft the copy.")
        out: CopyOutput
        if claude_workers():
            out = await claude_step.structured(
                worker=self.name,
                tier=worker_tier(tier, self.default_tier),
                system=INSTRUCTIONS,
                user=prompt,
                schema=CopyOutput,
                max_tokens=MAX_TOKENS,
            )
        else:
            out = (await self._agent.arun(prompt)).content
        return {
            "summary": out.hero_headline,
            "hero": {"headline": out.hero_headline, "subtitle": out.hero_subtitle},
            "sections": [s.model_dump() for s in out.sections],
            "counts": {"sections": len(out.sections)},
        }
