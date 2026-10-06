"""One run of each built-in Claude worker, through the real worker classes.

    python -m evals.orchestrator workers --live --max-usd 3 --out DIR

A sample, not a benchmark: each of the seven model workers runs once on its
default tier with a realistic intent from the dataset, and the run records
what came back (the artifact's title, size and validator result; the copy,
brief, research, tokens or audit summary), how long it took and what it cost
— every call booked at list price by the same recording transport the
pipeline eval uses. code.critic polishes code.gen's own draft, as it does in
a real run. Nothing is graded; the outputs are saved for a person to read.

The spend gate is the ceiling, not an estimate: the run is refused unless
every worker writing its full `max_tokens` would still fit under `--max-usd`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cost
from .app_pipeline import RecordingClaude, _CaseLog, _current

# A contract small enough for the 500-character intent bound, with one
# classic reentrancy bug (state written after the external call).
SAMPLE_CONTRACT = (
    "pragma solidity ^0.8.20; contract TipJar { mapping(address=>uint) public bal; "
    "function deposit() external payable { bal[msg.sender] += msg.value; } "
    "function withdraw() external { uint a = bal[msg.sender]; "
    '(bool ok,) = msg.sender.call{value: a}(""); require(ok); bal[msg.sender] = 0; } }'
)


@dataclass(frozen=True)
class Job:
    agent_id: str
    case_id: str | None  # the dataset case the intent comes from, when it does
    intent: str
    rationale: str
    max_tokens: int  # the worker's own budget, for the ceiling
    model: str  # its default tier's model, for the ceiling


JOBS: tuple[Job, ...] = (
    Job(
        "agt_01h8",
        "mod-001",
        "Build a landing page for my Pilates studio in BGC with a hero, class schedule, pricing and a contact form.",
        "Landing page copy: hero, schedule, pricing and contact sections.",
        8_000,
        "claude-haiku-4-5",
    ),
    Job(
        "agt_05x7",
        "low-006",
        "Write a 5-bullet SEO brief for a blog post about budget travel in Siargao.",
        "Keyword and audience brief for the post.",
        8_000,
        "claude-haiku-4-5",
    ),
    Job(
        "agt_02k2",
        "low-008",
        "Create a color palette (5 hex codes) for a calm meditation app.",
        "Design tokens for a calm, accessible palette.",
        8_000,
        "claude-haiku-4-5",
    ),
    Job(
        "agt_09l5",
        "mod-002",
        "Research the top 5 competitors of a meal-prep delivery startup in Manila and write a one-page comparison.",
        "Competitor research with sources and a comparison.",
        12_000,
        "claude-sonnet-5-5",
    ),
    Job(
        "agt_11c0",
        "mod-008",
        "Make an expense tracker web app where I can add expenses by category and see a pie chart.",
        "Single-file HTML app: add expenses by category, pie chart.",
        48_000,
        "claude-sonnet-5-5",
    ),
    Job(
        "agt_12r0",
        "mod-008",
        "Make an expense tracker web app where I can add expenses by category and see a pie chart.",
        "Polish the draft: accessibility, layout, edge cases.",
        48_000,
        "claude-sonnet-5-5",
    ),
    Job(
        "agt_04m1",
        None,
        f"Audit this Solidity contract for reentrancy and access-control issues: {SAMPLE_CONTRACT}",
        "Security review of the user's own contract before deployment.",
        16_000,
        "claude-opus-5-5",
    ),
)


def ceiling_usd(jobs: tuple[Job, ...] = JOBS) -> float:
    """Every worker writing its whole budget, plus a generous 6k-token prompt
    (code.critic reads code.gen's full draft)."""
    return sum(cost.call_cost(j.model, 6_000 + j.max_tokens, j.max_tokens) for j in jobs)


def _digest(agent_id: str, out: dict[str, Any]) -> dict[str, Any]:
    """The parts of a worker's output a reader needs, without the bulk."""
    digest: dict[str, Any] = {"summary": out.get("summary"), "counts": out.get("counts")}
    artifact = out.get("artifact")
    if isinstance(artifact, dict):
        digest["artifact_title"] = artifact.get("title")
        digest["artifact_files"] = [f.get("path") for f in artifact.get("files", [])]
    for key in ("validator_violations", "critic_violations", "critic_notes", "findings", "cvss_estimate"):
        if key in out:
            digest[key] = out[key]
    for key in ("hero", "sections", "keywords", "audiences", "palette", "typography", "sources"):
        if key in out:
            digest[key] = out[key]
    return digest


async def run_sample(out_dir: Path) -> dict[str, Any]:
    from app.agents.registry import WORKERS
    from app.llm import claude, spend

    spend.set_ledger(spend.SpendLedger(spend.InMemorySpendStore()))
    claude.set_transport(RecordingClaude(claude.get_transport()))
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    context: dict[str, Any] = {}
    for job in JOBS:
        worker = WORKERS[job.agent_id]
        log = _CaseLog()
        token = _current.set(log)
        started = time.perf_counter()
        error = None
        out: dict[str, Any] = {}
        try:
            out = await worker.run(job.intent, job.rationale, dict(context))
        except Exception as e:  # a worker failure is a result to report, not a crash
            error = f"{type(e).__name__}: {e}"
        finally:
            _current.reset(token)
        elapsed = time.perf_counter() - started
        if job.agent_id == "agt_11c0" and out:
            context["code.gen"] = out  # the critic polishes this draft
        artifact = out.get("artifact") if isinstance(out.get("artifact"), dict) else None
        if artifact:
            stem = worker.name.replace(".", "_")
            for f in artifact.get("files", []):
                path = out_dir / f"{stem}__{Path(str(f.get('path', 'index.html'))).name}"
                path.write_text(str(f.get("content", "")), encoding="utf-8")
        record = {
            "agent_id": job.agent_id,
            "worker": worker.name,
            "case_id": job.case_id,
            "intent": job.intent,
            "latency_s": round(elapsed, 2),
            "cost_usd": sum(c.cost_usd for c in log.calls),
            "calls": [
                {
                    "model": c.model,
                    "response_model": c.response_model,
                    "served_by": c.served_by,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "cache_read_input_tokens": c.cache_read_input_tokens,
                    "cache_creation_input_tokens": c.cache_creation_input_tokens,
                    "cost_usd": c.cost_usd,
                    "app_cost_usd": c.app_cost_usd,
                    "latency_ms": round(c.latency_ms),
                    "stop_reason": c.stop_reason,
                }
                for c in log.calls
            ],
            "error": error,
            "output": _digest(job.agent_id, out),
        }
        results[worker.name] = record
        (out_dir / f"{worker.name.replace('.', '_')}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return results
