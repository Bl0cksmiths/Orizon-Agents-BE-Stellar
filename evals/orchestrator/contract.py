"""What the harness needs from a pipeline run, independent of how it was produced.

A `Pipeline` takes one intent and reports what each stage of the orchestrator
did with it: the guard's verdict and the jev scores behind it, the improved
Spec and its re-check, and the RAW plan — the planner's own answer before
`orchestrator_svc` clamps it to the allowlist, because a clamped plan is valid
by construction and would score 100% on a planner that invents agents.

Three implementations: `AppPipeline` (the real guard, improver and planner,
over FakeJev/FakeClaude or, with `--live`, the real services), and the oracle
and null pipelines the harness is checked against (`synthetic.py`).

Score keys the guard is expected to report (orchestrator-v2's jev battery):

    injection            Noul 0..1
    harmful              Noul 0..1
    severity     Score on the 4-point rubric (0..3)
    real_request         Noul 0..1
    complexity_confidence  confidence of the complexity Choice
    recheck_injection    injection Noul on the improved Spec (when it ran)
    recheck_harmful      harmful Noul on the improved Spec (when it ran)
    same_request         Noul: the Spec asks for the same thing as the intent

The raw complexity choice (before the low-confidence round-up) travels as
`GuardObservation.raw_tier`, so a sweep can replay the round-up rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

SCORE_KEYS = (
    "injection",
    "harmful",
    "severity",
    "real_request",
    "complexity_confidence",
    "recheck_injection",
    "recheck_harmful",
    "same_request",
)


class PipelineUnavailable(Exception):
    """The run produced no scorable answer: the guard failed closed (both jev
    and the Haiku fallback down), a model stayed unavailable after its retries,
    or the per-case ceiling fired. Recorded in `errors.jsonl`, never scored —
    an unavailable guard is not a guard that blocked."""

    def __init__(self, failure_class: str, detail: str, calls: list[StageCall] | None = None) -> None:
        super().__init__(f"{failure_class}: {detail}")
        self.failure_class = failure_class
        self.detail = detail
        # Calls that completed (and were billed) before the failure.
        self.calls = calls or []


class SpendCapReached(Exception):
    """The app's own daily spend cap stopped the call (`planning_paused`)."""


@dataclass(frozen=True)
class StageCall:
    """One billed model call."""

    stage: str  # guard | guard_fallback | improve | recheck | plan
    model: str  # what was asked for
    served_by: str | None = None  # the model that answered, when it differs (server-side fallback)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0  # at list price for the model that answered
    latency_ms: float = 0.0
    stop_reason: str | None = None
    response_model: str | None = None  # the id the API answered with (a dated snapshot, say)
    app_cost_usd: float | None = None  # what the app's own spend ledger booked for this call
    effort: str | None = None  # the output effort the request asked for (None: not sent)
    first_token_ms: float | None = None  # streamed calls: time to the first text delta


@dataclass(frozen=True)
class GuardObservation:
    verdict: str  # allow | block | needs_detail
    tier: str | None  # the routed tier, after any round-up; None unless allowed
    raw_tier: str | None = None
    reasons: tuple[str, ...] = ()
    scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanObservation:
    """The planner's raw answer and what it was offered."""

    offered: frozenset[str]  # the agent ids in AVAILABLE_AGENTS for this call
    raw: dict[str, Any] | None  # the plan as the model returned it, dumped to JSON types
    refused: str | None = None  # refusal category when the model declined
    truncated: bool = False


@dataclass
class CaseRun:
    guard: GuardObservation
    spec: dict[str, Any] | None = None
    plan: PlanObservation | None = None
    calls: list[StageCall] = field(default_factory=list)
    # Ordered turns for traces/<id>_rep<k>.json: {role, content, name?}.
    transcript: list[dict[str, str]] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


class Pipeline(Protocol):
    name: str
    # True only when the pipeline spends real money.
    live: bool

    async def run(self, intent: str, *, stages: str) -> CaseRun:
        """Run `intent` through the guard, and through improve + plan when
        `stages == "all"` and the guard allowed it. Raises `PipelineUnavailable`
        or `SpendCapReached` instead of returning a run it could not complete."""
        ...
