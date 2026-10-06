from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import lazy_agent
from .base import Worker
from .prompt_safety import worker_prompt


class Section(BaseModel):
    title: str
    body: str = Field(..., max_length=280)


class CopyOutput(BaseModel):
    hero_headline: str
    hero_subtitle: str
    sections: list[Section] = Field(..., min_length=2, max_length=5)


class Copywrite(Worker):
    id = "agt_01h8"
    name = "copywrite.v3"
    real = True

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="copywrite.v3",
            model_id=settings.worker_model,
            instructions=(
                "You are a senior marketing copywriter. Given an intent, draft a hero "
                "headline (<=80 chars), a hero subtitle (<=160 chars), and 3–4 landing "
                "sections with a title and short body each. Punchy, concrete, outcome-focused."
            ),
            output_schema=CopyOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = worker_prompt(intent, rationale, "Draft the copy.")
        result = await self._agent.arun(prompt)
        out: CopyOutput = result.content
        return {
            "summary": out.hero_headline,
            "hero": {"headline": out.hero_headline, "subtitle": out.hero_subtitle},
            "sections": [s.model_dump() for s in out.sections],
            "counts": {"sections": len(out.sections)},
        }
