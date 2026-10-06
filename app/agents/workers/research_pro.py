from __future__ import annotations

import asyncio
import random
from typing import Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import lazy_agent
from .base import Worker
from .prompt_safety import worker_prompt


class Finding(BaseModel):
    claim: str = Field(..., max_length=200)
    confidence: float = Field(..., ge=0, le=1)


class ResearchOutput(BaseModel):
    findings: list[Finding] = Field(..., min_length=3, max_length=6)
    sources: list[str] = Field(..., max_length=6)
    summary: str = Field(..., max_length=300)


class ResearchPro(Worker):
    id = "agt_09l5"
    name = "research.pro"
    real = True

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="research.pro",
            model_id=settings.worker_model,
            instructions=(
                "You are a research synthesis agent. Given an intent, return 3–6 findings "
                "(each a concrete claim + 0..1 confidence), 2–6 plausible source descriptors "
                "(short strings, no fabricated URLs), and a one-paragraph summary. Mark "
                "confidence low when a claim is speculative."
            ),
            output_schema=ResearchOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kit = (context or {}).get("kit")

        # ── Kit fast path: deterministic feature brief, no LLM ──────────────
        if kit:
            await asyncio.sleep(0.35 + random.random() * 0.25)
            features = kit.get("features", []) or []
            findings = [
                {
                    "claim": f"{f['label']} — {f['detail']}"[:200],
                    "confidence": 0.95,
                }
                for f in features[:6]
            ]
            sources = [
                "internal feature brief",
                "kit playbook",
                "demo-bar checklist",
            ]
            kit_id = kit.get("kit_id", "kit")
            summary = (
                f"Locked {len(findings)} must-have features for the {kit_id} build "
                f"(kit playbook). Confidence is high because every item is a deterministic "
                f"requirement, not an estimate."
            )
            return {
                "summary": summary,
                "findings": findings,
                "sources": sources,
                "features_locked": len(findings),
                "counts": {"findings": len(findings), "sources": len(sources)},
                "source": f"kit:{kit_id}",
            }

        # ── Free-form path: LLM ─────────────────────────────────────────────
        prompt = worker_prompt(intent, rationale, "Return the research brief.")
        result = await self._agent.arun(prompt)
        out: ResearchOutput = result.content
        return {
            "summary": out.summary,
            "findings": [f.model_dump() for f in out.findings],
            "sources": out.sources,
            "counts": {"findings": len(out.findings), "sources": len(out.sources)},
            "source": "llm",
        }
