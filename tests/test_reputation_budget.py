"""The reputation read budget has to stay a small share of the planning budget.

decompose() reads reputation serially, ahead of the wait_for that bounds the
planning call, so a batch bound sized near decompose_timeout_seconds adds its
full cost to every plan during a Soroban outage — the exact incident that bound
was written to survive, and one that healthy RPC hides until it happens. The
validator refuses that configuration at boot; these pin both directions of it,
because either number can be moved on its own by a deployment that never looks
at the other.

Constructed with _env_file=None so the local .env can never leak into
assertions."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.config import REPUTATION_READ_BUDGET_SHARE, Settings
from app.services import reputation_svc


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _allowance(**overrides) -> float:
    """The largest batch bound a config permits, computed the validator's way.

    Derived rather than hard-coded so the boundary cases below are exact to the
    last bit of float, and so tuning REPUTATION_READ_BUDGET_SHARE moves the
    tests with the rule instead of leaving them asserting a stale 9 s.
    """
    return _settings(**overrides).decompose_timeout_seconds * REPUTATION_READ_BUDGET_SHARE


def test_the_shipped_config_boots_with_room_to_spare():
    s = _settings()
    assert s.reputation_batch_timeout_seconds == 2.5
    assert s.decompose_timeout_seconds == 90.0
    # 2.8% of the planning budget against a 10% ceiling. The rule pins today's
    # relationship without being tight enough to trip on ordinary tuning — the
    # two cases below are the proof that it still has slack in both directions.
    assert s.reputation_batch_timeout_seconds < _allowance()


def test_ordinary_tuning_on_either_side_still_boots():
    assert _settings(reputation_batch_timeout_seconds=5.0).reputation_batch_timeout_seconds == 5.0
    assert _settings(decompose_timeout_seconds=30.0).decompose_timeout_seconds == 30.0


def test_the_setting_is_the_bound_a_read_actually_uses():
    """A validator that cannot see the value it validates is theatre.

    The bound was lifted out of fetch_reps' signature so this one can be
    checked at boot. While the service still carries its own literal default
    the number lives in two places, and two places drift — so pin them equal. A
    `None` default means fetch_reps resolves the bound from Settings and there
    is nothing left to drift; anything else (a different literal, or a
    parameter made required) is the drift this exists to catch.
    """
    default = inspect.signature(reputation_svc.fetch_reps).parameters["timeout_seconds"].default
    if default is not None:
        assert default == _settings().reputation_batch_timeout_seconds


def test_a_raised_reputation_bound_refuses_to_boot():
    # The failure the story is actually about: someone widens the read bound to
    # ride out a flaky RPC and hands a third of every plan's budget to a read
    # that is supposed to be invisible.
    with pytest.raises(ValidationError, match="REPUTATION_BATCH_TIMEOUT_SECONDS"):
        _settings(reputation_batch_timeout_seconds=30.0)


def test_a_lowered_decompose_timeout_refuses_to_boot():
    # The same breach approached from the other side, and the one a review of
    # today's numbers would never catch: the read never changed, the budget it
    # has to fit inside shrank under it. 2.5 s is a quarter of a 10 s plan.
    with pytest.raises(ValidationError, match="DECOMPOSE_TIMEOUT_SECONDS"):
        _settings(decompose_timeout_seconds=10.0)


def test_exactly_at_the_margin_boots():
    # The rule is "at most this share", so the boundary itself is legal — a
    # deployment that lands on it is configured, not broken.
    exact = _allowance()
    assert _settings(reputation_batch_timeout_seconds=exact).reputation_batch_timeout_seconds == exact


def test_one_step_past_the_margin_does_not():
    with pytest.raises(ValidationError, match="REPUTATION_BATCH_TIMEOUT_SECONDS"):
        _settings(reputation_batch_timeout_seconds=_allowance() + 0.01)


def test_the_error_names_both_values_and_both_ways_out():
    with pytest.raises(ValidationError) as exc:
        _settings(reputation_batch_timeout_seconds=45.0, decompose_timeout_seconds=60.0)
    message = str(exc.value)
    # An operator staring at a failed deploy log has to be able to act on it
    # without opening app/config.py: both variable names, both values as
    # configured, the ceiling they broke, and the number that would satisfy it
    # from either side. 45 s of a 60 s budget; 6 s allowed; 450 s to keep 45.
    assert "REPUTATION_BATCH_TIMEOUT_SECONDS=45" in message
    assert "DECOMPOSE_TIMEOUT_SECONDS=60" in message
    assert "at most 6 s is allowed" in message
    assert "REPUTATION_BATCH_TIMEOUT_SECONDS to 6 or less" in message
    assert "DECOMPOSE_TIMEOUT_SECONDS to at least 450" in message
