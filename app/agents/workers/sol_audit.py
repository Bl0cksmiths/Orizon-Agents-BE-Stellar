from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import claude_workers, lazy_agent
from . import claude_step
from .base import ModelWorker
from .bounds import at_most, clamp, trim_text
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier

Severity = Literal["info", "low", "medium", "high", "critical"]


MAX_RATIONALE_CHARS = 240
MAX_SUMMARY_CHARS = 280
MAX_FINDINGS = 6
CVSS_MIN, CVSS_MAX = 0.0, 10.0


class AuditFinding(BaseModel):
    severity: Severity
    title: str
    rationale: str = Field(..., max_length=MAX_RATIONALE_CHARS)


class AuditOutput(BaseModel):
    summary: str = Field(..., max_length=MAX_SUMMARY_CHARS)
    findings: list[AuditFinding] = Field(..., max_length=MAX_FINDINGS)
    cvss_estimate: float = Field(..., ge=CVSS_MIN, le=CVSS_MAX)


class AuditFindingDraft(BaseModel):
    severity: Severity
    title: str
    rationale: str = Field(..., description="Why it matters, under 240 characters.")


class AuditDraft(BaseModel):
    """What Claude is asked for: AuditOutput's shape with no hard bounds, which
    structured outputs cannot enforce (see `bounds`); `fit_audit` applies them."""

    summary: str = Field(..., description="Under 280 characters.")
    findings: list[AuditFindingDraft] = Field(..., description="Up to 6 findings, most severe first.")
    cvss_estimate: float = Field(..., description="CVSS-style estimate from 0 to 10.")


def fit_audit(draft: AuditDraft) -> AuditOutput:
    """Trim, cap and clamp a draft into AuditOutput."""
    return AuditOutput(
        summary=trim_text(draft.summary, MAX_SUMMARY_CHARS),
        findings=[
            AuditFinding(
                severity=f.severity,
                title=" ".join(f.title.split()),
                rationale=trim_text(f.rationale, MAX_RATIONALE_CHARS),
            )
            for f in at_most(draft.findings, MAX_FINDINGS)
        ],
        cvss_estimate=clamp(draft.cvss_estimate, CVSS_MIN, CVSS_MAX),
    )


INSTRUCTIONS = (
    "You are a smart contract security auditor. Given an intent or contract "
    "description, return up to 6 findings (severity, title, rationale) and a "
    "CVSS-style estimate 0..10. If you lack the source, return severity='info' "
    "findings describing typical risks for the contract shape."
)

# Room for the JSON plus the deeper thinking an audit step does first.
MAX_TOKENS = 16_000


class SolAudit(ModelWorker):
    id = "agt_04m1"
    name = "sol-audit"
    real = True
    default_tier = "complex"

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="sol-audit",
            model_id=settings.worker_model,
            instructions=INSTRUCTIONS,
            output_schema=AuditOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
    ) -> dict[str, Any]:
        prompt = worker_prompt(intent, rationale, "Return the audit summary.")
        out: AuditOutput
        if claude_workers():
            draft = await claude_step.structured(
                worker=self.name,
                tier=self.effective_tier(tier),
                system=INSTRUCTIONS,
                user=prompt,
                schema=AuditDraft,
                max_tokens=MAX_TOKENS,
            )
            out = fit_audit(draft)
        else:
            out = (await self._agent.arun(prompt)).content
        return {
            "summary": out.summary,
            "findings": [f.model_dump() for f in out.findings],
            "cvss_estimate": out.cvss_estimate,
            "counts": {"findings": len(out.findings)},
        }
