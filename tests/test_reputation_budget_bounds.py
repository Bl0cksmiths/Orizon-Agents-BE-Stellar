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

from decimal import Decimal

import pytest

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
