from __future__ import annotations

import asyncio
import random
from typing import Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import lazy_agent
from .base import Worker
from .prompt_safety import worker_prompt


class SeoBriefOutput(BaseModel):
    keywords: list[str] = Field(..., max_length=12)
    audiences: list[str] = Field(..., max_length=5)
    summary: str


class SeoBrief(Worker):
    id = "agt_05x7"
    name = "seo.brief"
    real = True

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="seo.brief",
            model_id=settings.worker_model,
            instructions=(
                "You are an SEO research agent. Given an intent, return a JSON brief with: "
                "8–12 high-intent keywords, 2–4 audience clusters (concise labels), and a "
                "one-line summary. Be concrete, skip fluff."
            ),
            output_schema=SeoBriefOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
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
        result = await self._agent.arun(prompt)
        out: SeoBriefOutput = result.content
        return {
            "summary": out.summary,
            "keywords": out.keywords,
            "audiences": out.audiences,
            "counts": {"keywords": len(out.keywords), "audiences": len(out.audiences)},
            "source": "llm",
        }
