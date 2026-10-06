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

from ..llm.errors import SpendCapReached

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
