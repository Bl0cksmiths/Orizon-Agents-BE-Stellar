"""The reputation read bound has a floor, and its ceiling is exact where it is typed.

tests/test_reputation_budget.py pins the ceiling: one batched reputation read
may claim at most REPUTATION_READ_BUDGET_SHARE of the planning budget. These pin
the three places that rule was wrong at its edges.

It only looked up. A bound of 0, below 0 or NaN booted, and asyncio.wait_for
expires such a deadline on arrival — every reputation read gave up before it
started, every agent was scored on the prior, every plan was flagged
reputation_degraded, and the routing floor failed open for the life of the
process with the chain perfectly healthy.

It compared in binary rather than in the decimals an operator types. The
ceiling is a float product, so 0.07 typed against a 0.7 s budget — exactly 10% —
sat an ulp above 0.7 × 0.1 and was refused, along with 53 more of the first
2000 tenths-of-a-second budgets.

And its error message printed its own advice to six significant digits, so the
value it told an operator to set could be refused in turn.

Constructed with _env_file=None so the local .env can never leak into
assertions."""

from __future__ import annotations

import math
import re
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import REPUTATION_READ_BUDGET_SHARE, Settings


def _settings(**overrides: float) -> Settings:
    return Settings(_env_file=None, **overrides)


def _typed_share(decompose: float) -> float:
    """The bound an operator types for exactly the share of `decompose`.

    Worked in decimal and parsed once, the way an environment variable
    arrives: the number a person writes for 10% of 0.7 is 0.07, which is not
    the float 0.7 * 0.1 — and the gap between those two is the defect.
    Derived from the constant rather than hard-coded, so tuning the share
    moves these tests with the rule.
    """
    return float(Decimal(repr(decompose)) * Decimal(repr(REPUTATION_READ_BUDGET_SHARE)))


# ── a bound at exactly the share boots ──────────────────────────


@pytest.mark.parametrize("decompose", [0.7, 1.4, 2.3, 2.8, 4.6, 5.6, 9.2, 11.2])
def test_a_bound_typed_at_exactly_the_share_boots(decompose):
    """The budgets the audit found refusing their own 10%. Each typed bound
    parses to a float one ulp above decompose × share, so a strict `>`
    refused a configuration the rule explicitly permits."""
    bound = _typed_share(decompose)
    s = _settings(decompose_timeout_seconds=decompose, reputation_batch_timeout_seconds=bound)
    assert s.reputation_batch_timeout_seconds == bound


def test_every_budget_admits_a_bound_at_exactly_its_share():
    """0.1 s to 200 s in 0.1 s steps — every integer budget included — each
    with its bound typed at exactly the share. The named cases above are the
    ones someone found; this is the claim that there are none left."""
    above_the_float_product = 0
    for tenths in range(1, 2001):
        decompose = tenths / 10
        bound = _typed_share(decompose)
        above_the_float_product += bound > decompose * REPUTATION_READ_BUDGET_SHARE
        _settings(decompose_timeout_seconds=decompose, reputation_batch_timeout_seconds=bound)
    # Without this the sweep could pass vacuously: it only proves the
    # comparison is tolerant if some typed bound really does sit above the
    # float ceiling, which is the case a strict `>` refuses.
    assert above_the_float_product > 0


@pytest.mark.parametrize(("decompose", "bound"), [(25.0, 2.5), (90.0, 9.0), (56.0, 5.6), (10.0, 1.0), (1.0, 0.1)])
def test_the_real_boundaries_still_boot(decompose, bound):
    # The tolerance exists for fractional budgets; it must not have cost the
    # round-number ones a single boot. 90 / 9 is the shipped budget at its
    # full share.
    assert _settings(decompose_timeout_seconds=decompose, reputation_batch_timeout_seconds=bound)


@pytest.mark.parametrize("decompose", [0.7, 5.6, 25.0, 56.0, 90.0])
def test_the_tolerance_admits_no_bound_anyone_would_type(decompose):
    """Tolerant is not loose. One part in a billion absorbs float noise; a
    millisecond over, or one part in a million over, is a real breach and is
    refused exactly as before."""
    exact = _typed_share(decompose)
    for past in (exact + 0.001, exact * (1 + 1e-6)):
        with pytest.raises(ValidationError, match="REPUTATION_BATCH_TIMEOUT_SECONDS"):
            _settings(decompose_timeout_seconds=decompose, reputation_batch_timeout_seconds=past)


# ── the refusal's advice is advice the validator accepts ────────

# The two ways out the message offers, captured as an operator would copy them.
_LOWER_THE_BOUND_TO = re.compile(r"Lower REPUTATION_BATCH_TIMEOUT_SECONDS to (\S+) or less")
_RAISE_THE_BUDGET_TO = re.compile(r"raise DECOMPOSE_TIMEOUT_SECONDS to at least (\S+?)\.(?:\s|$)")


def _advice(decompose: float, bound: float) -> tuple[float, float]:
    """(suggested bound, suggested budget) from the refusal of this config."""
    with pytest.raises(ValidationError) as exc:
        _settings(decompose_timeout_seconds=decompose, reputation_batch_timeout_seconds=bound)
    message = str(exc.value)
    lower, higher = _LOWER_THE_BOUND_TO.search(message), _RAISE_THE_BUDGET_TO.search(message)
    assert lower and higher, message
    return float(lower.group(1)), float(higher.group(1))


def test_advice_is_not_rounded_past_the_ceiling_it_names():
    """The six-digit trap. A 123.456789 s budget allows 12.3456789 s, which
    `:g` printed as 12.3457 — above the ceiling — so an operator who did
    exactly what the deploy log said got the same refusal back."""
    bound, budget = _advice(123.456789, 20.0)
    assert bound == 12.3456789
    assert _settings(decompose_timeout_seconds=123.456789, reputation_batch_timeout_seconds=bound)
    assert _settings(decompose_timeout_seconds=budget, reputation_batch_timeout_seconds=20.0)


def test_the_suggested_bound_always_boots():
    """Every tenth-second budget from 0.1 s to 200 s, refused with a bound
    half again past its share: lowering the bound to exactly what the message
    says must boot. This is where the float trap and the rounding trap met —
    the suggestion was the float ceiling itself, so it failed on the same
    budgets a typed exact share did."""
    for tenths in range(1, 2001):
        decompose = tenths / 10
        suggested_bound, _ = _advice(decompose, _typed_share(decompose) * 1.5)
        _settings(decompose_timeout_seconds=decompose, reputation_batch_timeout_seconds=suggested_bound)


def test_the_suggested_budget_always_boots():
    """The other way out: keep the bound, raise the planning budget to what
    the message says. Every hundredth-of-a-second bound from 0.11 s to 20 s
    against a 1 s budget, so each one is refused and each gets advice."""
    for hundredths in range(11, 2001):
        bound = hundredths / 100
        _, suggested_budget = _advice(1.0, bound)
        _settings(decompose_timeout_seconds=suggested_budget, reputation_batch_timeout_seconds=bound)


# ── the bound has a floor ───────────────────────────────────────


@pytest.mark.parametrize("bound", [0.0, -0.0, -0.5, -2.5, math.nan, math.inf, -math.inf])
def test_a_bound_that_is_not_a_positive_duration_refuses_to_boot(bound):
    """Each of these booted before, and each deletes the bound: 0, anything
    negative and NaN expire before a read can answer; inf never expires."""
    with pytest.raises(ValidationError, match="is not a positive, finite number of seconds"):
        _settings(reputation_batch_timeout_seconds=bound)


def test_the_floor_refusal_names_the_value_the_consequence_and_the_fix():
    """Nothing about a zero bound looks broken from outside — reads "succeed"
    at the prior — so the deploy log has to say what it would have done: the
    floor stops filtering. And name a number that works, not just the rule."""
    with pytest.raises(ValidationError) as exc:
        _settings(reputation_batch_timeout_seconds=0.0)
    message = str(exc.value)
    assert "REPUTATION_BATCH_TIMEOUT_SECONDS=0 " in message
    assert "reputation_degraded" in message
    assert "routing floor stops filtering anyone" in message
    assert f"the default is {Settings.model_fields['reputation_batch_timeout_seconds'].default:g}" in message


@pytest.mark.parametrize("typed", ["0", "-1", "nan", "NaN", "inf", "-inf"])
def test_the_floor_holds_for_the_strings_a_dashboard_sends(monkeypatch, typed):
    # Render hands the process a string. pydantic parses "nan" and "inf" to
    # floats without complaint, so the refusal has to hold on the path a
    # deployment actually takes, not only for Python literals.
    monkeypatch.setenv("REPUTATION_BATCH_TIMEOUT_SECONDS", typed)
    with pytest.raises(ValidationError, match="REPUTATION_BATCH_TIMEOUT_SECONDS"):
        _settings()
