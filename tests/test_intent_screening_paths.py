"""`intent_screening.screen_free_form`, driven directly with stubbed checks.

Pins what the decompose suites reach only through the planner: the spend cap
reached at the re-check pauses planning on both the improver's and the buyer's
spec, and a refusal while the improver is still writing cancels it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.llm.errors import SpendCapReached
from app.services import intent_guard, intent_screening, prompt_improver
from app.services.intent_guard import GuardDecision
from app.services.prompt_improver import Spec

CAP = SpendCapReached(spent_usd=10.0, cap_usd=10.0, retry_after=120)
SPEC = Spec(goal="A bakery site", deliverable="one page", summary="A warm one-page site for a bakery.")


def _guard(monkeypatch: pytest.MonkeyPatch, decision: GuardDecision) -> None:
    async def check_intent(intent: str) -> GuardDecision:
        await asyncio.sleep(0)  # the improver starts while the guard reads
        return decision

    monkeypatch.setattr(intent_guard, "check_intent", check_intent)


def _recheck_hits_the_cap(monkeypatch: pytest.MonkeyPatch) -> list[Spec]:
    checked: list[Spec] = []

    async def recheck(original: str, spec: Spec, **kw: Any) -> Any:
        checked.append(spec)
        raise CAP

    monkeypatch.setattr(prompt_improver, "recheck", recheck)
    return checked


def test_the_cap_reached_at_the_recheck_of_an_improved_spec_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def improve(intent: str) -> Spec:
        return SPEC

    monkeypatch.setattr(intent_screening, "_improve", improve)
    _guard(monkeypatch, GuardDecision(verdict="allow", tier="low"))
    checked = _recheck_hits_the_cap(monkeypatch)

    with pytest.raises(intent_screening.PlanningPaused) as caught:
        asyncio.run(intent_screening.screen_free_form("a bakery site"))

    assert caught.value.retry_after == 120
    assert checked == [SPEC]


def test_the_cap_reached_at_the_recheck_of_an_edited_spec_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def improve(intent: str) -> Spec:
        raise AssertionError("an edited spec is not improved")

    monkeypatch.setattr(intent_screening, "_improve", improve)
    _guard(monkeypatch, GuardDecision(verdict="allow", tier="low"))
    checked = _recheck_hits_the_cap(monkeypatch)

    with pytest.raises(intent_screening.PlanningPaused):
        asyncio.run(intent_screening.screen_free_form("a bakery site", user_spec=SPEC))

    assert checked == [SPEC]


def test_a_refusal_while_the_improver_is_writing_cancels_it(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {}

    async def improve(intent: str) -> Spec:
        state["started"] = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return SPEC

    monkeypatch.setattr(intent_screening, "_improve", improve)
    _guard(monkeypatch, GuardDecision(verdict="block", message="no"))

    async def scenario() -> None:
        with pytest.raises(intent_screening.IntentBlocked):
            await intent_screening.screen_free_form("something unsafe")
        await asyncio.sleep(0)  # let the cancellation land and its callback run
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert state == {"started": True, "cancelled": True}
