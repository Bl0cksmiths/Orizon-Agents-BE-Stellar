"""Screening: what an intent passes before anything is planned from it.

The Claude pipeline's first half, between the request and the planner:

    jev guard  ─┐                      (concurrently; the improver's work is
    improver   ─┴─► spec re-check ─► resolve        discarded if the guard says no)

and the one place a verdict becomes a refusal. The guard and the improver
belong to `intent_guard` / `prompt_improver`; this module only wires them and
decides what the buyer is told. It never plans: `orchestrator_svc` takes the
`Screening` it returns, or the typed refusal it raises, from here.

Fail closed throughout. A guard that cannot answer is `IntentUnavailable`,
never an allow; a spec that cannot be re-checked is not planned from; and the
daily spend cap is `PlanningPaused` for anything that would reach the planner.
The one carve-out is the owner's: a curated demo kit still plans while the cap
is reached, because its plan is fixed and no model reads the intent to make it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..llm.errors import LLMError, SpendCapReached
from ..llm.tiers import Tier, display_name, improver_model
from ..schemas import PlanStage
from . import intent_guard, prompt_improver
from .intent_guard import GuardDecision
from .prompt_improver import Resolution, Spec, SpecCheck

logger = logging.getLogger(__name__)

# The improver runs beside the guard, before the guard's tier is known, so it
# writes against this one. The tier only sizes the spec (how many constraints
# and done criteria), never what it asks for, and the planner gets the real
# tier — so a provisional middle costs nothing a re-run would buy back.
PROVISIONAL_IMPROVER_TIER: Tier = "moderate"

PAUSED_MESSAGE = (
    "AI planning is paused for today: the daily AI budget has been used. "
    "Curated demos (Tetris, Snake, Calculator, Pomodoro) still work, and planning resumes at 00:00 UTC."
)
PLANNER_DECLINED_MESSAGE = (
    "We can't plan this request. If it is a legitimate task, try describing the work you want done more plainly."
)


class IntentRefused(Exception):
    """A request the pipeline will not plan, with what to tell the buyer.

    `status` and `code` are the HTTP answer; `message` is plain, user-facing
    wording safe to show as is; `retry_after` is seconds, for the 503s.
    """

    status: int = 422
    code: str = "intent_refused"

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class IntentBlocked(IntentRefused):
    """The request check (or the planner itself) declined the request."""

    code = "intent_blocked"


class IntentNeedsDetail(IntentRefused):
    """Not recognisably a task yet; `message` is the question to ask back."""

    code = "intent_needs_detail"


class IntentUnavailable(IntentRefused):
    """Neither jev nor its Claude stand-in could check the request. Retryable."""

    status = 503
    code = "intent_unavailable"


class PlanningPaused(IntentRefused):
    """Today's AI spend has reached the cap. Retryable once the UTC day turns."""

    status = 503
    code = "planning_paused"


def paused(err: SpendCapReached) -> PlanningPaused:
    return PlanningPaused(PAUSED_MESSAGE, retry_after=max(1, int(err.retry_after)))


@dataclass(frozen=True)
class Screening:
    """A request cleared to plan, and how it got there.

    `spec` is what the planner plans from when it is set — the improved or
    buyer-edited reading, re-checked clean — and the original intent when it
    is None. `decision` is None only when the check was skipped: a curated kit
    under the spend cap.
    """

    decision: GuardDecision | None
    spec: Spec | None
    improved_by: str | None
    stages: tuple[PlanStage, ...]

    @property
    def tier(self) -> Tier | None:
        return self.decision.tier if self.decision is not None else None

    @property
    def guard_model(self) -> str | None:
        return self.decision.model if self.decision is not None else None


def _checked_by(decision: GuardDecision) -> str:
    if decision.source == "fallback" and decision.model:
        return f"{display_name(decision.model)}, standing in for jev"
    return "jev"


def _guard_stage(decision: GuardDecision) -> PlanStage:
    return PlanStage(stage="guard", msg=f"Request checked by {_checked_by(decision)} (tier: {decision.tier})")


def _refuse_unless_allowed(decision: GuardDecision) -> None:
    """Raise the refusal a non-allow verdict means; return on allow."""
    if decision.verdict == "allow" and decision.tier is not None:
        return
    if decision.verdict == "block":
        raise IntentBlocked(decision.message or PLANNER_DECLINED_MESSAGE)
    if decision.verdict == "needs_detail":
        raise IntentNeedsDetail(decision.message or "What would you like the agents to do?")
    # "unavailable", and an allow without a tier, which the guard never
    # produces and which nothing could plan with: closed, not open.
    raise IntentUnavailable(
        decision.message or "We couldn't check this request just now. Please try again shortly.",
        retry_after=decision.retry_after_s or intent_guard.RETRY_AFTER_SECONDS,
    )


def _refuse_unless_planned(resolution: Resolution) -> None:
    if resolution.action in ("use_spec", "use_original"):
        return
    if resolution.action == "block":
        raise IntentBlocked(resolution.message or PLANNER_DECLINED_MESSAGE)
    if resolution.action == "needs_detail":
        raise IntentNeedsDetail(resolution.message or "What would you like the agents to do?")
    raise IntentUnavailable(
        resolution.message or "We couldn't check this request just now. Please try again shortly.",
        retry_after=resolution.retry_after_s or intent_guard.RETRY_AFTER_SECONDS,
    )


async def screen_kit(intent: str) -> Screening:
    """The request check for a curated demo intent: the guard alone.

    A kit's plan is fixed, so there is nothing to improve or re-check, and a
    `watch` verdict does not hold it up: the watch rule protects the planner
    from borderline text, and no planner reads this intent. A block, a
    needs-detail or an outage refuses it like any other request.
    """
    try:
        decision = await intent_guard.check_intent(intent)
    except SpendCapReached:
        # The owner's carve-out: demos run while AI planning is paused.
        logger.info("intent check skipped for a curated kit: the daily AI spend cap is reached")
        stage = PlanStage(stage="guard", msg="Request check paused: today's AI budget is spent; curated demo served")
        return Screening(decision=None, spec=None, improved_by=None, stages=(stage,))
    _refuse_unless_allowed(decision)
    return Screening(decision=decision, spec=None, improved_by=None, stages=(_guard_stage(decision),))


async def _improve(intent: str) -> Spec | None:
    """The improver's spec, or None when it could not write one.

    Any model-layer failure but the spend cap means "no improved spec":
    `resolve` then plans from the original the guard cleared, or refuses
    when the guard asked for a clean spec. The cap propagates.
    """
    try:
        return await prompt_improver.improve(intent, PROVISIONAL_IMPROVER_TIER)
    except SpendCapReached:
        raise
    except LLMError as err:
        logger.warning("prompt improver gave no spec (%s); planning falls back to the original", type(err).__name__)
        return None


def _recheck_stage(check: SpecCheck, resolution: Resolution, *, user_edited: bool) -> PlanStage:
    checked_by = "jev" if check.source != "fallback" else f"{display_name(check.model or '')}, standing in for jev"
    if resolution.action == "use_spec":
        subject = "Your edited request" if user_edited else "Improved request"
        return PlanStage(stage="recheck", msg=f"{subject} re-checked by {checked_by}")
    if check.verdict == "drifted":
        why = "The improved request drifted from yours"
    elif check.verdict == "unavailable":
        why = "The improved request could not be re-checked"
    else:
        why = "The improved request did not re-check clean"
    return PlanStage(stage="recheck", msg=f"{why}; planning from your own words")


async def screen_free_form(intent: str, *, user_spec: Spec | None = None) -> Screening:
    """Guard ∥ improver, then the re-check, then one decision. Raises a refusal or returns.

    With `user_spec` — the buyer's corrected reading of an earlier answer —
    the improver does not run: the spec is re-checked against the intent
    instead, and must read clean to be planned from (`resolve`'s
    `user_edited` rule).
    """
    improving: asyncio.Task[Spec | None] | None = None
    if user_spec is None:
        improving = asyncio.create_task(_improve(intent))
    try:
        try:
            decision = await intent_guard.check_intent(intent)
        except SpendCapReached as err:
            raise paused(err) from err
        # The improver's work is thrown away on any refusal: it was written
        # from text the guard would not let through.
        _refuse_unless_allowed(decision)
        stages = [_guard_stage(decision)]

        if improving is not None:
            try:
                spec = await improving
            except SpendCapReached as err:
                raise paused(err) from err
            if spec is not None:
                stages.append(PlanStage(stage="improve", msg=f"Prompt improved by {display_name(improver_model())}"))
            else:
                stages.append(PlanStage(stage="improve", msg="Prompt improvement unavailable"))
        else:
            spec = user_spec
            stages.append(PlanStage(stage="improve", msg="Using your edited reading of the request"))

        check: SpecCheck | None = None
        if spec is not None:
            try:
                check = await prompt_improver.recheck(intent, spec)
            except SpendCapReached as err:
                raise paused(err) from err
        resolution = prompt_improver.resolve(decision, check, user_edited=user_spec is not None)
        _refuse_unless_planned(resolution)
        if check is not None:
            stages.append(_recheck_stage(check, resolution, user_edited=user_spec is not None))
        planned = spec if resolution.action == "use_spec" else None
        logger.info(
            "intent screened: tier=%s watch=%s plan_from=%s reasons=%s",
            decision.tier,
            decision.watch,
            "spec" if planned is not None else "original",
            ",".join(resolution.reasons),
        )
        return Screening(
            decision=decision,
            spec=planned,
            improved_by=improver_model() if (improving is not None and spec is not None) else None,
            stages=tuple(stages),
        )
    finally:
        if improving is not None and not improving.done():
            # Not awaited: this frame may itself be unwinding a cancellation
            # (the decompose budget), which awaiting would swallow. The
            # callback retrieves the outcome so nothing is logged as lost.
            improving.add_done_callback(_discard)
            improving.cancel()


def _discard(task: asyncio.Task[Spec | None]) -> None:
    if not task.cancelled():
        task.exception()
