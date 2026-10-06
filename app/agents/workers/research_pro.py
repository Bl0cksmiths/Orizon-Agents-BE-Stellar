from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import claude_workers, lazy_agent
from . import claude_step
from .base import ModelWorker
from .bounds import at_most, clamp, trim_items, trim_text
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier


class Finding(BaseModel):
    claim: str = Field(..., max_length=200)  # MAX_CLAIM_CHARS
    confidence: float = Field(..., ge=0, le=1)


MIN_FINDINGS, MAX_FINDINGS = 3, 6
MAX_SOURCES = 6
MAX_CLAIM_CHARS = 200
MAX_SUMMARY_CHARS = 300


class ResearchOutput(BaseModel):
    findings: list[Finding] = Field(..., min_length=MIN_FINDINGS, max_length=MAX_FINDINGS)
    sources: list[str] = Field(..., max_length=MAX_SOURCES)
    summary: str = Field(..., max_length=MAX_SUMMARY_CHARS)


class FindingDraft(BaseModel):
    claim: str = Field(..., description="One concrete claim, one or two sentences, under 200 characters.")
    confidence: float = Field(..., description="0 to 1; low when the claim is speculative.")


class ResearchDraft(BaseModel):
    """What Claude is asked for: ResearchOutput's shape with no hard bounds,
    which structured outputs cannot enforce (see `bounds`). `fit_research`
    brings it inside ResearchOutput's."""

    findings: list[FindingDraft] = Field(..., description="3 to 6 findings.")
    sources: list[str] = Field(..., description="2 to 6 short source descriptors, no URLs you cannot vouch for.")
    summary: str = Field(..., description="One paragraph, under 300 characters.")


def fit_research(draft: ResearchDraft) -> ResearchOutput:
    """Trim, cap and clamp a draft into ResearchOutput; too few findings stays invalid."""
    findings = [
        Finding(claim=claim, confidence=clamp(f.confidence, 0.0, 1.0))
        for f in draft.findings
        if (claim := trim_text(f.claim, MAX_CLAIM_CHARS))
    ]
    if len(findings) < MIN_FINDINGS:
        raise claude_step.ModelStepError(
            claude_step.INVALID_OUTPUT, f"research.pro: {len(findings)} usable findings, needs {MIN_FINDINGS}"
        )
    return ResearchOutput(
        findings=at_most(findings, MAX_FINDINGS),
        sources=at_most(trim_items(draft.sources), MAX_SOURCES),
        summary=trim_text(draft.summary, MAX_SUMMARY_CHARS),
    )


INSTRUCTIONS = (
    "You are a research synthesis agent. Given an intent, return 3–6 findings "
    "(each a concrete claim + 0..1 confidence), 2–6 plausible source descriptors "
    "(short strings, no fabricated URLs), and a one-paragraph summary. Mark "
    "confidence low when a claim is speculative."
)

# Room for the JSON plus the thinking a synthesis step does first.
MAX_TOKENS = 12_000

# How to use what earlier steps handed on (see `context.CONSUMES`).
UPSTREAM_GUIDANCE = (
    "Research what they contain: extracted text or a translation is the "
    "material to research, and an audit's findings are context to explain. "
    "Mark confidence low for anything you cannot vouch for beyond them."
)


class ResearchPro(ModelWorker):
    id = "agt_09l5"
    name = "research.pro"
    real = True
    default_tier = "moderate"
    reads_upstream = True

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="research.pro",
            model_id=settings.worker_model,
            instructions=INSTRUCTIONS,
            output_schema=ResearchOutput,
        )

    def _deterministic(self, context: dict[str, Any] | None) -> bool:
        return bool((context or {}).get("kit"))

    def build_prompt(self, intent: str, rationale: str, context: dict[str, Any] | None = None) -> str:
        """The free-form prompt: the fenced request, then the fenced upstream outputs."""
        return worker_prompt(
            intent,
            rationale,
            "Return the research brief.",
            sections=[self.handoff(context).section(UPSTREAM_GUIDANCE)],
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
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
        prompt = self.build_prompt(intent, rationale, context)
        out: ResearchOutput
        if claude_workers():
            draft = await claude_step.structured(
                worker=self.name,
                tier=self.effective_tier(tier),
                system=INSTRUCTIONS,
                user=prompt,
                schema=ResearchDraft,
                max_tokens=MAX_TOKENS,
            )
            out = fit_research(draft)
        else:
            out = (await self._agent.arun(prompt)).content
        return {
            "summary": out.summary,
            "findings": [f.model_dump() for f in out.findings],
            "sources": out.sources,
            "counts": {"findings": len(out.findings), "sources": len(out.sources)},
            "source": "llm",
        }
