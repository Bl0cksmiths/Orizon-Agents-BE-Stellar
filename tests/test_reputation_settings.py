"""Every reputation setting is range-checked at boot, and the refusal never
quotes the value.

Only REPUTATION_BATCH_TIMEOUT_SECONDS used to be validated. The rest were taken
as typed, and pydantic parses nan, inf and any sign for a float: a NaN prior
weight crashed lifespan with "cannot convert float NaN to integer", a NaN
rating-weight cap switched the cap off, and an out-of-scale prior was served
to clients unclamped. Constructed with _env_file=None so the local .env can
never leak into assertions.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, Settings, _load_settings

_BAD_FLOATS = [math.nan, math.inf, -math.inf, -1.0, 0.0]

CASES = (
    [("reputation_prior_bps", v) for v in (-1, 10_001, 20_000, -100)]
    + [("reputation_floor_bps", v) for v in (-5, 10_001)]
    + [("reputation_prior_weight_usdc", v) for v in _BAD_FLOATS]
    + [("reputation_max_rating_weight_usdc", v) for v in _BAD_FLOATS]
    + [("reputation_read_ttl_seconds", v) for v in _BAD_FLOATS]
    + [("reputation_max_rating_to_prior_ratio", v) for v in _BAD_FLOATS]
    + [("reputation_stale_grace_seconds", v) for v in (math.nan, math.inf, -1.0)]
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_the_shipped_reputation_config_boots():
    _settings()


@pytest.mark.parametrize(("field", "value"), CASES)
def test_an_unusable_reputation_setting_refuses_to_boot(field, value):
    with pytest.raises(ValidationError, match=field.upper()):
        _settings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reputation_prior_bps", 0),
        ("reputation_prior_bps", 10_000),
        ("reputation_floor_bps", 0),
        ("reputation_floor_bps", 10_000),
        ("reputation_stale_grace_seconds", 0.0),
    ],
)
def test_the_edges_of_each_range_boot(field, value):
    assert getattr(_settings(**{field: value}), field) == value


@pytest.mark.parametrize(
    ("env", "typed"),
    [
        ("REPUTATION_PRIOR_WEIGHT_USDC", "nan"),
        ("REPUTATION_MAX_RATING_WEIGHT_USDC", "NaN"),
        ("REPUTATION_READ_TTL_SECONDS", "inf"),
        ("REPUTATION_PRIOR_BPS", "20000"),
        ("REPUTATION_FLOOR_BPS", "-5"),
    ],
)
def test_the_strings_a_dashboard_sends_are_refused_too(monkeypatch, env, typed):
    monkeypatch.setenv(env, typed)
    with pytest.raises(ValidationError, match=env):
        _settings()


def test_the_boot_failure_names_the_variable_and_never_the_value():
    """Through the real boot path, which is what reaches the deploy log."""
    with pytest.raises(ConfigurationError) as exc:
        _load_settings(_env_file=None, reputation_prior_weight_usdc=-12.345, reputation_floor_bps=10_987)
    message = str(exc.value)
    assert "REPUTATION_PRIOR_WEIGHT_USDC" in message
    assert "REPUTATION_FLOOR_BPS" in message
    assert "12.345" not in message
    assert "10987" not in message
