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

import dataclasses
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    tier: str | None = None  # the plan step's tier; None runs the worker's default
    typical_usd: float = 0.0  # what this job cost when last measured, for a budgeted run
    label: str | None = None  # names the record when a worker runs more than once
    critic_of: str | None = None  # code.critic: the label of the code.gen draft it polishes


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


# The release re-check (2026-10-06): the two workers whose prompts or schemas
# changed after the campaign, on the campaign's own inputs, and code.gen handed
# a complex-tier step — which its tier cap must run on Sonnet, not Opus.
# `typical_usd` is each job's campaign cost.
_BY_ID = {j.agent_id: j for j in JOBS}
RECHECK_JOBS: tuple[Job, ...] = (
    replace(_BY_ID["agt_01h8"], typical_usd=0.0018),
    replace(_BY_ID["agt_09l5"], typical_usd=0.0110),
    Job(
        "agt_11c0",
        "cpx-001",
        "Build a full booking system web app for a barbershop: services, staff schedules, time-slot booking, "
        "admin view, and email-style confirmations.",
        "Single-file HTML booking app: services, schedules, slots, admin view, confirmations.",
        48_000,
        "claude-sonnet-5-5",
        tier="complex",
        typical_usd=0.0937,
    ),
)


# A budgeted job starts only if its last measured cost, times this, fits.
HEADROOM = 1.25


# The complex code.gen re-measure after the length fix (code.gen and
# code.critic on Claude: ~250-450 lines, 9 000-token ceiling, low effort, a
# 100 s stream budget): two code.gen drafts of cpx-001 on a complex step, and
# code.critic on the first. Ordered so the critic runs before the second
# draft; a job that no longer fits the budget is skipped, not started.
_CPX_001 = RECHECK_JOBS[2]
CODE_LENGTH_JOBS: tuple[Job, ...] = (
    replace(_CPX_001, label="code.gen#1", typical_usd=0.06),
    Job(
        "agt_12r0",
        "cpx-001",
        _CPX_001.intent,
        "Polish the draft: accessibility, layout, edge cases.",
        9_000,
        "claude-sonnet-5-5",
        tier="complex",
        typical_usd=0.07,
        label="code.critic#1",
        critic_of="code.gen#1",
    ),
    replace(_CPX_001, label="code.gen#2", typical_usd=0.06),
)


class BudgetExceeded(Exception):
    """A streamed reply was cut off because it would have passed the budget."""

    def __init__(self, estimate_usd: float) -> None:
        super().__init__(f"stopped at an estimated ${estimate_usd:.4f}")
        self.estimate_usd = estimate_usd  # billed but never reported back: counted as spent


# Streamed text is about 3.5 characters a token; thinking is billed but not
# streamed, so the visible text is scaled up by the share code.gen's campaign
# call spent thinking (8,725 output tokens for ~5,400 of HTML).
_CHARS_PER_TOKEN = 3.5
_THINKING_FACTOR = 1.7


class BudgetedClaude:
    """Stops a streamed reply before its estimated cost passes the budget left.

    Non-streamed calls cannot be cut off, so a budgeted run only starts a job
    whose last measured cost fits; the long code.gen reply streams and is the
    one this guard can actually stop."""

    def __init__(self, inner: Any, remaining: Callable[[], float]) -> None:
        self.inner = inner
        self.remaining = remaining
        # The running estimate of the stream in flight, kept for a stream that
        # is aborted from outside (the worker's own wall-clock budget): billed,
        # never reported back, so the run books this figure instead.
        self.in_flight_usd = 0.0

    async def complete(self, request: Any) -> Any:
        if not request.stream:
            return await self.inner.complete(request)
        from app.llm import spend

        price = spend.price_for(request.model)
        prompt_usd = (len(request.system) + len(request.user)) / _CHARS_PER_TOKEN * price.input / 1_000_000
        seen = 0
        original = request.on_text

        async def on_text(delta: str) -> None:
            nonlocal seen
            seen += len(delta)
            spent = prompt_usd + seen / _CHARS_PER_TOKEN * _THINKING_FACTOR * price.output / 1_000_000
            self.in_flight_usd = spent
            if spent > self.remaining():
                raise BudgetExceeded(spent)
            if original is not None:
                maybe = original(delta)
                if inspect.isawaitable(maybe):
                    await maybe

        self.in_flight_usd = prompt_usd
        completion = await self.inner.complete(dataclasses.replace(request, on_text=on_text))
        self.in_flight_usd = 0.0  # reported: the recorder books the real figure
        return completion


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


async def run_sample(out_dir: Path, jobs: tuple[Job, ...] = JOBS, budget_usd: float | None = None) -> dict[str, Any]:
    """Run `jobs` in order. With `budget_usd`, a job starts only when its last
    measured cost fits what is left, and a streamed reply is cut off before it
    would pass the budget (`BudgetedClaude`)."""
    from app.agents.registry import WORKERS
    from app.llm import claude, spend

    spent = 0.0

    def remaining() -> float:
        return (budget_usd if budget_usd is not None else float("inf")) - spent

    spend.set_ledger(spend.SpendLedger(spend.InMemorySpendStore()))
    budgeted = BudgetedClaude(claude.get_transport(), remaining) if budget_usd is not None else None
    claude.set_transport(RecordingClaude(budgeted or claude.get_transport()))
    drafts: dict[str, dict[str, Any]] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    context: dict[str, Any] = {}
    for job in jobs:
        worker = WORKERS[job.agent_id]
        label = job.label or worker.name
        if budget_usd is not None and job.typical_usd * HEADROOM > remaining():
            results[label] = {"worker": worker.name, "label": label, "skipped": f"budget left {remaining():.4f} USD"}
            continue
        if job.critic_of is not None:
            context["code.gen"] = drafts.get(job.critic_of, {})
        log = _CaseLog()
        token = _current.set(log)
        started = time.perf_counter()
        error = None
        unreported = 0.0
        if budgeted is not None:
            budgeted.in_flight_usd = 0.0
        out: dict[str, Any] = {}
        try:
            if job.tier is not None:
                out = await worker.run(job.intent, job.rationale, dict(context), tier=job.tier)  # type: ignore[call-arg]
            else:
                out = await worker.run(job.intent, job.rationale, dict(context))
        except BudgetExceeded as e:
            error = f"BudgetExceeded: {e}"
            spent += e.estimate_usd
            unreported = e.estimate_usd
        except Exception as e:  # a worker failure is a result to report, not a crash
            error = f"{type(e).__name__}: {e}"
            if budgeted is not None and budgeted.in_flight_usd:
                # A stream aborted from outside (the worker's own wall-clock
                # budget): billed, never reported. Book the running estimate.
                unreported = budgeted.in_flight_usd
                spent += unreported
                error += f" (stream aborted; estimated ${unreported:.4f} unreported)"
        finally:
            _current.reset(token)
        elapsed = time.perf_counter() - started
        spent += sum(c.cost_usd for c in log.calls)
        if job.agent_id == "agt_11c0" and out:
            context["code.gen"] = out  # the critic polishes this draft
            drafts[label] = out
        artifact = out.get("artifact") if isinstance(out.get("artifact"), dict) else None
        if artifact:
            stem = label.replace(".", "_").replace("#", "_")
            for f in artifact.get("files", []):
                path = out_dir / f"{stem}__{Path(str(f.get('path', 'index.html'))).name}"
                path.write_text(str(f.get("content", "")), encoding="utf-8")
        record = {
            "agent_id": job.agent_id,
            "worker": worker.name,
            "label": label,
            "unreported_estimated_usd": unreported,
            "case_id": job.case_id,
            "tier": job.tier,
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
                    "first_token_ms": round(c.first_token_ms) if c.first_token_ms is not None else None,
                    "effort": c.effort,
                    "stop_reason": c.stop_reason,
                }
                for c in log.calls
            ],
            "error": error,
            "output": _digest(job.agent_id, out),
        }
        results[label] = record
        (out_dir / f"{label.replace('.', '_').replace('#', '_')}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return results
