"""PLAN_TTL_SECONDS refuses any value a buyer could not execute inside, or that never expires."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 0.0, 59.0, -900.0])
def test_an_unusable_plan_ttl_refuses_to_boot_and_names_only_its_variable(value: float) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(plan_ttl_seconds=value)
    message = info.value.errors()[0]["msg"]
    assert "PLAN_TTL_SECONDS" in message
    assert repr(value) not in message


@pytest.mark.parametrize("value", [60.0, 900.0, 3600.0])
def test_a_usable_plan_ttl_boots(value: float) -> None:
    assert Settings(plan_ttl_seconds=value).plan_ttl_seconds == value


def test_the_shipped_plan_ttl_is_fifteen_minutes() -> None:
    assert Settings().plan_ttl_seconds == 900.0
