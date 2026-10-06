"""Tiers: how hard a request is, and the Claude model and effort each one runs on.

The guard rates every request low / moderate / complex; a plan's steps carry a
tier too. Each tier names one model (env-overridable, exact Claude ids only)
and one effort. The pipeline's fixed roles reuse the tier models, so changing
a tier's model in the environment moves every role on it together:

    planner          complex tier's model (Claude Opus 5.5), effort by request tier
    prompt improver  moderate tier's model (Claude Sonnet 5.5)
    fallback guard   low tier's model (Claude Haiku 4.5), when jev cannot answer
"""

from __future__ import annotations

from typing import Literal, get_args

from ..config import settings

Tier = Literal["low", "moderate", "complex"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

TIERS: tuple[Tier, ...] = get_args(Tier)
EFFORTS: tuple[Effort, ...] = get_args(Effort)

_EFFORT: dict[Tier, Effort] = {"low": "low", "moderate": "medium", "complex": "high"}


def model_for(tier: Tier) -> str:
    """The Claude model id a step or request of this tier runs on."""
    if tier == "low":
        return settings.claude_model_low
    if tier == "moderate":
        return settings.claude_model_moderate
    if tier == "complex":
        return settings.claude_model_complex
    raise ValueError(f"unknown tier {tier!r}")


def effort_for(tier: Tier) -> Effort:
    """The output effort for this tier: low → low, moderate → medium, complex → high."""
    try:
        return _EFFORT[tier]
    except KeyError:
        raise ValueError(f"unknown tier {tier!r}") from None


def tier_up(tier: Tier) -> Tier:
    """One tier harder, saturating at complex (the guard's low-confidence rule)."""
    index = TIERS.index(tier)
    return TIERS[min(index + 1, len(TIERS) - 1)]


def planner_model() -> str:
    """The planner's model: the complex tier's (Claude Opus 5.5)."""
    return model_for("complex")


def improver_model() -> str:
    """The prompt improver's model: the moderate tier's (Claude Sonnet 5.5)."""
    return model_for("moderate")


def guard_fallback_model() -> str:
    """The fallback guard's model when jev cannot answer: the low tier's (Claude Haiku 4.5)."""
    return model_for("low")


_DISPLAY = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-4-5": "Claude Haiku 4.5",
}


def display_name(model: str) -> str:
    """The name a trace line or a plan card shows for a model id ("Claude Opus 5.5")."""
    return _DISPLAY.get(model, model)
