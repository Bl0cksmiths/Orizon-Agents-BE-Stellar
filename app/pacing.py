"""Randomized timing — demo "thinking" pauses and retry backoff jitter.

None of this is security: the values only spread out WHEN something happens,
never WHAT it produces. It still draws from the operating system's CSPRNG
(`secrets.SystemRandom`) rather than the `random` module's Mersenne Twister,
so the codebase holds no non-cryptographic generator that a later change could
reach for by accident when it needs an unpredictable value.

Every caller passes its range as `low + span`, so a pause is uniform over
`[low, low + span)` — the same shape as the `low + random.random() * span`
expressions this module replaced, with identical bounds.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Protocol


class UnitSource(Protocol):
    """Anything with a `random()` returning a float in `[0.0, 1.0)`.

    `secrets.SystemRandom` and `random.Random` both fit, which is what lets a
    test inject a seeded generator without this module importing `random`.
    """

    def random(self) -> float: ...


_SYSTEM: UnitSource = secrets.SystemRandom()


def spread(low: float, span: float, source: UnitSource | None = None) -> float:
    """A float uniform over `[low, low + span)`.

    Both bounds must be non-negative: a negative low or span is a caller bug,
    and as a sleep it would be a zero-length pause that hides it.
    """
    if low < 0 or span < 0:
        raise ValueError(f"pacing range must be non-negative, got low={low!r} span={span!r}")
    return low + (source or _SYSTEM).random() * span


def jitter(low: float = 0.5, span: float = 1.0, source: UnitSource | None = None) -> float:
    """A backoff multiplier uniform over `[low, low + span)` — `[0.5, 1.5)` by default."""
    return spread(low, span, source)


async def pause(low_s: float, span_s: float, source: UnitSource | None = None) -> None:
    """Sleep for a uniform `[low_s, low_s + span_s)` seconds."""
    await asyncio.sleep(spread(low_s, span_s, source))
