from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import claude_workers, lazy_agent, worker_tier
from . import claude_step
from .base import ModelWorker
from .bounds import at_most, trim_items
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier


MAX_KEYWORDS = 12
MAX_AUDIENCES = 5


class SeoBriefOutput(BaseModel):
    keywords: list[str] = Field(..., max_length=MAX_KEYWORDS)
    audiences: list[str] = Field(..., max_length=MAX_AUDIENCES)
    summary: str


class SeoBriefDraft(BaseModel):
    """What Claude is asked for: SeoBriefOutput's shape with no hard bounds,
    which structured outputs cannot enforce (see `bounds`); `fit_seo_brief`
    applies them."""

    keywords: list[str] = Field(..., description="8 to 12 high-intent keywords.")
    audiences: list[str] = Field(..., description="2 to 4 audience clusters, concise labels.")
    summary: str = Field(..., description="One line.")


def fit_seo_brief(draft: SeoBriefDraft) -> SeoBriefOutput:
    """Cap a draft's lists into SeoBriefOutput."""
    return SeoBriefOutput(
        keywords=at_most(trim_items(draft.keywords), MAX_KEYWORDS),
        audiences=at_most(trim_items(draft.audiences), MAX_AUDIENCES),
        summary=" ".join(draft.summary.split()),
    )


INSTRUCTIONS = (
    "You are an SEO research agent. Given an intent, return a JSON brief with: "
    "8–12 high-intent keywords, 2–4 audience clusters (concise labels), and a "
    "one-line summary. Be concrete, skip fluff."
)

# Room for the JSON plus any thinking the tier's model does first.
MAX_TOKENS = 8_000


class SeoBrief(ModelWorker):
    id = "agt_05x7"
    name = "seo.brief"
    real = True
    default_tier = "low"

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="seo.brief",
            model_id=settings.worker_model,
            instructions=INSTRUCTIONS,
            output_schema=SeoBriefOutput,
        )

    def _deterministic(self, context: dict[str, Any] | None) -> bool:
        return bool((context or {}).get("kit"))

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
    ) -> dict[str, Any]:
        kit = (context or {}).get("kit")

        # ── Kit fast path: deterministic brand block, no LLM ────────────────
        if kit:
            await asyncio.sleep(0.3 + random.random() * 0.2)
            brand = kit.get("brand", {}) or {}
            name = brand.get("name", "Artifact")
            tagline = brand.get("tagline", "")
            audience = brand.get("audience", []) or []
            keywords = brand.get("keywords", []) or []
            summary = f'name: "{name}" · tone: {tagline} · audience: {", ".join(audience)}'
            return {
                "summary": summary[:280],
                "brand_name": name,
                "tagline": tagline,
                "audiences": audience,
                "keywords": keywords,
                "counts": {
                    "keywords": len(keywords),
                    "audiences": len(audience),
                },
                "source": f"kit:{kit.get('kit_id', 'kit')}",
            }

        # ── Free-form path: LLM ─────────────────────────────────────────────
        prompt = worker_prompt(intent, rationale, "Return the SEO brief.")
        out: SeoBriefOutput
        if claude_workers():
            draft = await claude_step.structured(
                worker=self.name,
                tier=worker_tier(tier, self.default_tier),
                system=INSTRUCTIONS,
                user=prompt,
                schema=SeoBriefDraft,
                max_tokens=MAX_TOKENS,
            )
            out = fit_seo_brief(draft)
        else:
            out = (await self._agent.arun(prompt)).content
        return {
            "summary": out.summary,
            "keywords": out.keywords,
            "audiences": out.audiences,
            "counts": {"keywords": len(out.keywords), "audiences": len(out.audiences)},
            "source": "llm",
        }
