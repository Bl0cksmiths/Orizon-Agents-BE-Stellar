from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import claude_workers, lazy_agent, worker_tier
from . import claude_step
from .base import ModelWorker
from .bounds import at_most, trim_text
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier


MAX_BODY_CHARS = 280
MIN_SECTIONS, MAX_SECTIONS = 2, 5


class Section(BaseModel):
    title: str
    body: str = Field(..., max_length=MAX_BODY_CHARS)


class CopyOutput(BaseModel):
    hero_headline: str
    hero_subtitle: str
    sections: list[Section] = Field(..., min_length=MIN_SECTIONS, max_length=MAX_SECTIONS)


class SectionDraft(BaseModel):
    title: str
    body: str = Field(..., description="Short body copy, under 280 characters.")


class CopyDraft(BaseModel):
    """What Claude is asked for: CopyOutput's shape with no hard bounds, which
    structured outputs cannot enforce (see `bounds`); `fit_copy` applies them."""

    hero_headline: str = Field(..., description="At most 80 characters.")
    hero_subtitle: str = Field(..., description="At most 160 characters.")
    sections: list[SectionDraft] = Field(..., description="3 to 4 landing sections.")


def fit_copy(draft: CopyDraft) -> CopyOutput:
    """Trim and cap a draft into CopyOutput; fewer than two sections stays invalid."""
    sections = [
        Section(title=" ".join(sec.title.split()), body=body)
        for sec in draft.sections
        if (body := trim_text(sec.body, MAX_BODY_CHARS))
    ]
    if len(sections) < MIN_SECTIONS:
        raise claude_step.ModelStepError(
            claude_step.INVALID_OUTPUT, f"copywrite.v3: {len(sections)} usable sections, needs {MIN_SECTIONS}"
        )
    return CopyOutput(
        hero_headline=" ".join(draft.hero_headline.split()),
        hero_subtitle=" ".join(draft.hero_subtitle.split()),
        sections=at_most(sections, MAX_SECTIONS),
    )


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
            draft = await claude_step.structured(
                worker=self.name,
                tier=worker_tier(tier, self.default_tier),
                system=INSTRUCTIONS,
                user=prompt,
                schema=CopyDraft,
                max_tokens=MAX_TOKENS,
            )
            out = fit_copy(draft)
        else:
            out = (await self._agent.arun(prompt)).content
        return {
            "summary": out.hero_headline,
            "hero": {"headline": out.hero_headline, "subtitle": out.hero_subtitle},
            "sections": [s.model_dump() for s in out.sections],
            "counts": {"sections": len(out.sections)},
        }
