from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import claude_workers, lazy_agent, worker_tier
from . import claude_step
from .base import ModelWorker
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier

Severity = Literal["info", "low", "medium", "high", "critical"]


class AuditFinding(BaseModel):
    severity: Severity
    title: str
    rationale: str = Field(..., max_length=240)


class AuditOutput(BaseModel):
    summary: str = Field(..., max_length=280)
    findings: list[AuditFinding] = Field(..., max_length=6)
    cvss_estimate: float = Field(..., ge=0, le=10)


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
            out = await claude_step.structured(
                worker=self.name,
                tier=worker_tier(tier, self.default_tier),
                system=INSTRUCTIONS,
                user=prompt,
                schema=AuditOutput,
                max_tokens=MAX_TOKENS,
            )
        else:
            out = (await self._agent.arun(prompt)).content
        return {
            "summary": out.summary,
            "findings": [f.model_dump() for f in out.findings],
            "cvss_estimate": out.cvss_estimate,
            "counts": {"findings": len(out.findings)},
        }
