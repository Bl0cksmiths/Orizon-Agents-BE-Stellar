from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import lazy_agent
from .base import Worker
from .prompt_safety import worker_prompt

Severity = Literal["info", "low", "medium", "high", "critical"]


class AuditFinding(BaseModel):
    severity: Severity
    title: str
    rationale: str = Field(..., max_length=240)


class AuditOutput(BaseModel):
    summary: str = Field(..., max_length=280)
    findings: list[AuditFinding] = Field(..., max_length=6)
    cvss_estimate: float = Field(..., ge=0, le=10)


class SolAudit(Worker):
    id = "agt_04m1"
    name = "sol-audit"
    real = True

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="sol-audit",
            model_id=settings.worker_model,
            instructions=(
                "You are a smart contract security auditor. Given an intent or contract "
                "description, return up to 6 findings (severity, title, rationale) and a "
                "CVSS-style estimate 0..10. If you lack the source, return severity='info' "
                "findings describing typical risks for the contract shape."
            ),
            output_schema=AuditOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = worker_prompt(intent, rationale, "Return the audit summary.")
        result = await self._agent.arun(prompt)
        out: AuditOutput = result.content
        return {
            "summary": out.summary,
            "findings": [f.model_dump() for f in out.findings],
            "cvss_estimate": out.cvss_estimate,
            "counts": {"findings": len(out.findings)},
        }
