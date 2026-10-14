"""Which error the jev spec re-check raises when its concurrent calls fail.

The re-check asks jev two things at once — the spec's safety, and whether it
is still the same request — and awaits both even when one fails, so no request
is orphaned. Then exactly one error surfaces: the spend cap outranks anything
else, so the caller pauses instead of falling back; otherwise the first
failure in (safety, same) order.

A `RuntimeError` stands in for "some other failure": `recheck` does not catch
it, so the exact instance raised by `_jev_recheck` reaches the test and its
identity proves which call it came from. Everything runs on the offline fakes.
"""

from __future__ import annotations

import asyncio

import pytest

from app.llm import testing
from app.llm.errors import JevUnavailable, SpendCapReached
from app.services.prompt_improver import Spec, recheck

INTENT = "Build a landing page for my bakery in Cebu, with the menu and opening hours."
SPEC = Spec(
    goal="Give the Cebu bakery a web presence.",
    deliverable="A single landing page.",
    constraints=["Show the menu", "Show the opening hours"],
    done_criteria=["The page lists the menu and the hours"],
    summary="A landing page for a Cebu bakery.",
)
SAFETY = "guard.spec"
SAME = "guard.spec.same"
CALLS = (SAFETY, SAME)


def _spend_cap() -> SpendCapReached:
    return SpendCapReached(spent_usd=10, cap_usd=10, retry_after=60)


def _answer(fake_jev: testing.FakeJev, purpose: str) -> None:
    if purpose == SAFETY:
        fake_jev.answer({"injection": 0.02, "harmful": 0.01, "severity": testing.score(0)}, purpose=SAFETY)
    else:
        fake_jev.answer({"same_request": 0.95}, purpose=SAME)


def _other(purpose: str) -> str:
    return SAME if purpose == SAFETY else SAFETY


@pytest.mark.parametrize("capped", CALLS)
@pytest.mark.parametrize(
    "other_failure",
    [lambda: JevUnavailable("timeout"), lambda: RuntimeError("boom")],
    ids=["outage", "unexpected"],
)
def test_the_spend_cap_on_either_call_outranks_any_other_failure(fake_jev, fake_claude, capped, other_failure):
    cap = _spend_cap()
    fake_jev.fail(cap, purpose=capped)
    fake_jev.fail(other_failure(), purpose=_other(capped))

    with pytest.raises(SpendCapReached) as raised:
        asyncio.run(recheck(INTENT, SPEC))

    assert raised.value is cap
    assert fake_claude.calls == []  # paused, never re-run on the fallback
    assert len(fake_jev.calls_for(SAFETY)) == len(fake_jev.calls_for(SAME)) == 1


@pytest.mark.parametrize("failing", CALLS)
def test_one_failure_is_raised_as_itself_after_both_calls_ran(fake_jev, failing):
    failure = RuntimeError(f"{failing} failed")
    fake_jev.fail(failure, purpose=failing)
    _answer(fake_jev, _other(failing))

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(recheck(INTENT, SPEC))

    assert raised.value is failure
    # The healthy call was still awaited, not orphaned by its partner's error.
    assert len(fake_jev.calls_for(SAFETY)) == len(fake_jev.calls_for(SAME)) == 1


def test_two_ordinary_failures_raise_the_safety_call_s_first(fake_jev):
    safety_failure = RuntimeError("safety failed")
    same_failure = RuntimeError("same failed")
    fake_jev.fail(safety_failure, purpose=SAFETY)
    fake_jev.fail(same_failure, purpose=SAME)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(recheck(INTENT, SPEC))

    assert raised.value is safety_failure


def test_two_spend_caps_raise_the_safety_call_s_first(fake_jev):
    safety_cap, same_cap = _spend_cap(), _spend_cap()
    fake_jev.fail(safety_cap, purpose=SAFETY)
    fake_jev.fail(same_cap, purpose=SAME)

    with pytest.raises(SpendCapReached) as raised:
        asyncio.run(recheck(INTENT, SPEC))

    assert raised.value is safety_cap


def test_both_calls_answering_is_judged_from_jev(fake_jev, fake_claude):
    _answer(fake_jev, SAFETY)
    _answer(fake_jev, SAME)

    result = asyncio.run(recheck(INTENT, SPEC))

    assert result.verdict == "clean"
    assert result.source == "jev"
    assert result.scores == {"same_request": 0.95, "injection": 0.02, "harmful": 0.01, "severity": 0}
    assert fake_claude.calls == []
