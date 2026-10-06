"""The Claude/OpenAI switch, and what /readiness says about the model layer.

ORCHESTRATOR_PROVIDER picks the stack the orchestrator plans and works on:
"anthropic" or "openai" by name, or — empty or "auto", the default — Claude
once ANTHROPIC_API_KEY is set and OpenAI until then. The OpenAI/agno path
stays behind this switch until Claude is proven live.

`readiness()` is the probe's view: config and in-memory figures only, no
network call. Anonymous callers see whether planning is active or paused;
the spend figures, keys and models are for the operator API key only.
Only the active provider's key decides `llm` (and with it whether
/readiness answers 503); jev's key is informational, because the guard
falls back to Claude without it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from ..config import settings
from . import spend, tiers

Provider = Literal["anthropic", "openai"]


def active_provider() -> Provider:
    """The provider the orchestrator runs on now."""
    choice = settings.orchestrator_provider.strip().lower()
    if choice == "anthropic":
        return "anthropic"
    if choice == "openai":
        return "openai"
    return "anthropic" if settings.anthropic_api_key.strip() else "openai"


def provider_key_present(provider: Provider | None = None) -> bool:
    """Whether `provider` (default: the active one) has its API key."""
    chosen = provider or active_provider()
    key = settings.anthropic_api_key if chosen == "anthropic" else settings.openai_api_key
    return bool(key.strip())


class LLMModels(BaseModel):
    """The model ids in force, by role and by tier."""

    planner: str
    improver: str
    guard: str  # the jev model
    guard_fallback: str
    low: str
    moderate: str
    complex: str


class LLMSpend(BaseModel):
    """Today's spend (UTC) as this process holds it, against the cap."""

    day: str
    spent_usd: float
    cap_usd: float
    paused: bool  # the cap is reached: AI planning answers planning_paused


class LLMPublicReadiness(BaseModel):
    """The model layer as anyone may see it on /readiness.

    Whether AI planning is running, and when a pause lifts — never how much
    has been spent or what the cap is: those numbers would tell an abuser
    exactly how much budget is left to burn. The operator view adds them.
    """

    provider: Provider
    planning: Literal["active", "paused"]
    resets_at: str | None  # ISO-8601 UTC, the next UTC midnight, only while paused


class LLMReadiness(BaseModel):
    """The model layer on /readiness for a caller holding the operator API key.

    Informational: `llm` alone gates readiness. Key fields say present or
    not, never any part of a key.
    """

    provider: Provider
    planning: Literal["active", "paused"]
    resets_at: str | None
    anthropic_key: bool
    typesafe_key: bool
    models: LLMModels
    spend: LLMSpend


def _resets_at() -> str:
    moment = datetime.now(UTC) + timedelta(seconds=spend.seconds_until_reset())
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def readiness(*, operator: bool = False) -> LLMReadiness | LLMPublicReadiness:
    """The probe's view: config and the ledger's in-memory total, no I/O.

    Anonymous callers get `LLMPublicReadiness`; `operator=True` (the caller
    presented the operator API key) the full report. A stale total starts
    one background re-read for the next probe to see.
    """
    spend.refresh_if_stale()
    today = spend.snapshot()
    planning: Literal["active", "paused"] = "paused" if today.paused else "active"
    resets_at = _resets_at() if today.paused else None
    if not operator:
        return LLMPublicReadiness(provider=active_provider(), planning=planning, resets_at=resets_at)
    return LLMReadiness(
        provider=active_provider(),
        planning=planning,
        resets_at=resets_at,
        anthropic_key=bool(settings.anthropic_api_key.strip()),
        typesafe_key=bool(settings.typesafe_api_key.strip()),
        models=LLMModels(
            planner=tiers.planner_model(),
            improver=tiers.improver_model(),
            guard=settings.typesafe_model,
            guard_fallback=tiers.guard_fallback_model(),
            low=tiers.model_for("low"),
            moderate=tiers.model_for("moderate"),
            complex=tiers.model_for("complex"),
        ),
        spend=LLMSpend(day=today.day, spent_usd=today.spent_usd, cap_usd=today.cap_usd, paused=today.paused),
    )


async def close() -> None:
    """Release the model layer at shutdown: this loop's SDK clients and the ledger's pool.

    A transport that was never used holds nothing and is not created here.
    """
    from . import claude, jev

    for transport in (claude._transport, jev._transport):
        aclose = getattr(transport, "aclose", None)
        if aclose is not None:
            await aclose()
    await spend.close_spend_store()
