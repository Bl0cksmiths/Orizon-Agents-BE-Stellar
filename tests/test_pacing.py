"""The shared pacing helper: uniform ranges, the OS CSPRNG, no negative sleeps."""

from __future__ import annotations

import asyncio
import random
import secrets

import pytest

from app import pacing


class _Fixed:
    """A unit source that always returns the same draw."""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


@pytest.mark.parametrize(("draw", "expected"), [(0.0, 0.4), (0.5, 0.7), (0.999, 0.4 + 0.999 * 0.6)])
def test_spread_maps_the_unit_draw_onto_low_plus_span(draw: float, expected: float) -> None:
    assert pacing.spread(0.4, 0.6, _Fixed(draw)) == pytest.approx(expected)


def test_spread_stays_inside_its_half_open_range() -> None:
    rng = random.Random(1234)
    draws = [pacing.spread(1.4, 1.0, rng) for _ in range(2000)]
    assert all(1.4 <= d < 2.4 for d in draws)


def test_a_seeded_source_makes_the_draws_reproducible() -> None:
    first = [pacing.spread(0.2, 0.4, random.Random(7)) for _ in range(3)]
    second = [pacing.spread(0.2, 0.4, random.Random(7)) for _ in range(3)]
    assert first == second


@pytest.mark.parametrize(("low", "span"), [(-0.1, 0.5), (0.1, -0.5), (-1.0, -1.0)])
def test_a_negative_range_is_refused_rather_than_slept_as_zero(low: float, span: float) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        pacing.spread(low, span)


def test_a_zero_span_is_a_fixed_value() -> None:
    assert pacing.spread(0.3, 0.0) == 0.3


def test_the_default_source_is_the_os_csprng() -> None:
    assert isinstance(pacing._SYSTEM, secrets.SystemRandom)


def test_the_default_source_draws_inside_the_range() -> None:
    draws = [pacing.spread(0.25, 0.15) for _ in range(500)]
    assert all(0.25 <= d < 0.40 for d in draws)


def test_jitter_defaults_to_half_to_one_and_a_half() -> None:
    assert pacing.jitter(source=_Fixed(0.0)) == 0.5
    assert pacing.jitter(source=_Fixed(0.5)) == 1.0
    draws = [pacing.jitter() for _ in range(500)]
    assert all(0.5 <= d < 1.5 for d in draws)


def test_pause_sleeps_for_the_spread_value(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    asyncio.run(pacing.pause(0.28, 0.18, _Fixed(0.5)))
    assert slept == [pytest.approx(0.37)]


def test_pause_refuses_a_negative_range_before_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    with pytest.raises(ValueError):
        asyncio.run(pacing.pause(-0.1, 0.2))
    assert slept == []
