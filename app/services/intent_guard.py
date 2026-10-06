"""Intent guard: decide whether a console request may reach the planner.

One jev (TypeSafe) call answers a five-question battery about the raw intent —
injection, harmful + severity, real request, complexity — and the thresholds
below turn those probabilities into a verdict:

* ``block``        injection ≥ 0.40, or harmful ≥ 0.70, or severity ≥ 2
* ``needs_detail`` real_request < 0.30 (gibberish, pings, a lone keyword)
* ``allow``        everything else, with a tier; injection in [0.35, 0.40)
                   marks the decision ``watch``, and the request may then only
                   proceed through an improved spec whose own re-check reads
                   clean (see ``prompt_improver.resolve``)
* ``unavailable``  jev AND the fallback are down — fail closed, never open

The tier is jev's complexity choice, rounded up one step when its confidence is
below 0.5: under-provisioning a hard task costs a failed run, over-provisioning
an easy one costs a few cents.

When jev cannot answer (timeout, error, missing key, malformed answer) the same
questions go to Claude Haiku 4.5 with structured output. The fallback reads the
intent inside the standard untrusted-data fence because, unlike jev, it is an
instruction-following model; jev reads the text bare because it is a classifier
and the fence's marker redaction would hide a forgery attempt from it.

Thresholds live in ``GuardPolicy`` so the evals harness can sweep them without
patching. The injection block line is the live-eval pick (2026-10-06 report,
sweep.md): 0.40 caught 75% of held-out injections against 67% at the starting
0.70, at the same 1.7% false-block rate.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.agents.workers.prompt_safety import fence_untrusted
from app.llm import claude, jev
from app.llm.errors import JevUnavailable, LLMError, LLMRefused, SpendCapReached
from app.llm.tiers import Tier, guard_fallback_model, tier_up

from . import intent_guard_prompts as prompts

logger = logging.getLogger(__name__)

Verdict = Literal["allow", "block", "needs_detail", "unavailable"]
GuardSource = Literal["jev", "fallback"]

# Bounds the text either classifier sees. Intents are already capped at 500
# characters by the request model; a user-edited spec renders to a few times
# that. jev's own limit (32k tokens of state + question) is far above this.
MAX_STATE_CHARS = 6000

# Seconds the console should wait before retrying when both guards are down.
RETRY_AFTER_SECONDS = 30

_FALLBACK_MAX_TOKENS = 400
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class GuardPolicy:
    """Decision thresholds.

    Injection has two lines: at or above ``injection_block`` a request is
    blocked; from ``injection_watch`` up to (not including) the block line it
    is allowed under ``watch`` and must proceed through a clean spec. With the
    block line at 0.40 the watch band is the narrow [0.35, 0.40). The same two
    lines judge an improved spec's re-check. A watch line above the block line
    would leave no band at all and is refused.
    """

    injection_block: float = 0.40
    injection_watch: float = 0.35
    harmful_block: float = 0.70
    severity_block: float = 2.0
    real_request_min: float = 0.30
    complexity_confidence_min: float = 0.5
    same_request_min: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 <= self.injection_watch <= self.injection_block <= 1.0:
            raise ValueError("GuardPolicy needs 0 <= injection_watch <= injection_block <= 1")


DEFAULT_POLICY = GuardPolicy()


class GuardDecision(BaseModel):
    """The guard's verdict on one intent.

    ``message`` is plain, user-facing wording for every verdict except allow:
    the block reason, the needs-detail question, or the try-again notice.
    ``reasons`` are machine codes (``injection``, ``harmful``, ``severity``,
    ``unclear``, ``watch``, ``tier_rounded_up``, ``fallback``, ``refused``,
    ``guard_unavailable``) for traces and evals; they never reach users raw.
    """

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    tier: Tier | None = None
    watch: bool = False
    reasons: list[str] = []
    message: str | None = None
    scores: dict[str, float] = {}
    source: GuardSource | None = None
    model: str | None = None
    retry_after_s: int | None = None


class IntentAssessment(BaseModel):
    """Structured output the Claude fallback returns for the intent battery."""

    model_config = ConfigDict(extra="forbid")

    injection: float
    harmful: float
    severity: int
    real_request: float
    complexity: Tier
    complexity_confidence: float


@dataclass(frozen=True)
class Scores:
    injection: float
    harmful: float
    severity: float
    real_request: float
    complexity: Tier
    complexity_confidence: float

    def as_dict(self) -> dict[str, float]:
        return {
            "injection": self.injection,
            "harmful": self.harmful,
            "severity": self.severity,
            "real_request": self.real_request,
            "complexity_confidence": self.complexity_confidence,
        }


class MalformedAnswer(ValueError):
    """A classifier answered, but not with the shape the battery asked for."""


def unit(value: Any, name: str) -> float:
    """A probability in [0, 1], or MalformedAnswer — never a silent default."""
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        raise MalformedAnswer(f"{name} is not a number: {value!r}")
    return min(1.0, max(0.0, float(value)))


def level(value: Any, name: str, top: int = 3) -> float:
    """A rubric level in [0, top], or MalformedAnswer."""
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        raise MalformedAnswer(f"{name} is not a number: {value!r}")
    return min(float(top), max(0.0, float(value)))


def answer(answers: Mapping[str, Any], qid: str, attr: str) -> Any:
    item = answers.get(qid) if isinstance(answers, Mapping) else None
    if item is None or not hasattr(item, attr):
        raise MalformedAnswer(f"no {attr} answer for {qid!r}")
    return getattr(item, attr)


def classifier_state(text: str) -> str:
    """Raw text for jev: control characters out, length bounded, nothing redacted."""
    cleaned = _CONTROL_CHARS.sub(" ", text or "").strip()
    return cleaned[:MAX_STATE_CHARS]


def _scores_from_jev(answers: Mapping[str, Any]) -> Scores:
    complexity = answer(answers, "complexity", "choice")
    if complexity not in ("low", "moderate", "complex"):
        raise MalformedAnswer(f"complexity choice {complexity!r} is not a tier")
    return Scores(
        injection=unit(answer(answers, "injection", "noul"), "injection"),
        harmful=unit(answer(answers, "harmful", "noul"), "harmful"),
        severity=level(answer(answers, "severity", "score"), "severity"),
        real_request=unit(answer(answers, "real_request", "noul"), "real_request"),
        complexity=complexity,
        complexity_confidence=unit(answer(answers, "complexity", "confidence"), "complexity confidence"),
    )


def _scores_from_fallback(value: IntentAssessment) -> Scores:
    return Scores(
        injection=unit(value.injection, "injection"),
        harmful=unit(value.harmful, "harmful"),
        severity=level(value.severity, "severity"),
        real_request=unit(value.real_request, "real_request"),
        complexity=value.complexity,
        complexity_confidence=unit(value.complexity_confidence, "complexity confidence"),
    )


def decide(
    scores: Scores, *, source: GuardSource, model: str | None, policy: GuardPolicy = DEFAULT_POLICY
) -> GuardDecision:
    """Turn one assessment into a verdict. Pure — the whole policy lives here."""
    common: dict[str, Any] = {"scores": scores.as_dict(), "source": source, "model": model}
    base_reasons = ["fallback"] if source == "fallback" else []

    blocking: list[str] = []
    if scores.injection >= policy.injection_block:
        blocking.append("injection")
    if scores.harmful >= policy.harmful_block:
        blocking.append("harmful")
    if scores.severity >= policy.severity_block:
        blocking.append("severity")
    if blocking:
        # An injection attempt is named as such even when it is also harmful:
        # the console's advice differs ("describe the job" vs "we can't help").
        message = prompts.BLOCKED_INJECTION if "injection" in blocking else prompts.BLOCKED_HARMFUL
        return GuardDecision(verdict="block", reasons=blocking + base_reasons, message=message, **common)

    if scores.real_request < policy.real_request_min:
        return GuardDecision(
            verdict="needs_detail",
            reasons=["unclear", *base_reasons],
            message=prompts.NEEDS_DETAIL_QUESTION,
            **common,
        )

    reasons = list(base_reasons)
    tier: Tier = scores.complexity
    if scores.complexity_confidence < policy.complexity_confidence_min:
        rounded = tier_up(tier)
        if rounded != tier:
            reasons.append("tier_rounded_up")
        tier = rounded
    watch = scores.injection >= policy.injection_watch
    if watch:
        reasons.append("watch")
    return GuardDecision(verdict="allow", tier=tier, watch=watch, reasons=reasons, **common)


def unavailable(*, model: str | None = None) -> GuardDecision:
    return GuardDecision(
        verdict="unavailable",
        reasons=["guard_unavailable"],
        message=prompts.UNAVAILABLE_MESSAGE,
        model=model,
        retry_after_s=RETRY_AFTER_SECONDS,
    )


def _refused(model: str | None) -> GuardDecision:
    # The fallback classifier declining to even read the text is itself the
    # strongest harm signal we get — fail closed as a block.
    return GuardDecision(
        verdict="block",
        reasons=["harmful", "refused", "fallback"],
        message=prompts.BLOCKED_HARMFUL,
        source="fallback",
        model=model,
    )


async def _jev_scores(state: str) -> tuple[Scores, str]:
    result = await jev.ask(purpose="guard.intent", state=state, questions=prompts.INTENT_BATTERY)
    return _scores_from_jev(result.answers), result.model


async def _fallback_decision(intent: str, policy: GuardPolicy) -> GuardDecision:
    model = guard_fallback_model()
    try:
        result = await claude.structured(
            purpose="guard.intent.fallback",
            model=model,
            system=prompts.INTENT_FALLBACK_SYSTEM,
            user=fence_untrusted(intent, label="USER_REQUEST", max_chars=MAX_STATE_CHARS),
            schema=IntentAssessment,
            max_tokens=_FALLBACK_MAX_TOKENS,
            # Like jev, the guard is not held to the daily cap: a pass costs a
            # fraction of a cent, and curated demo kits must still clear it
            # after AI planning has paused.
            enforce_cap=False,
        )
        scores = _scores_from_fallback(result.value)
    except SpendCapReached:
        raise
    except LLMRefused as exc:
        logger.warning("guard fallback refused (category=%s)", getattr(exc, "category", None))
        return _refused(model)
    except (LLMError, MalformedAnswer) as exc:
        logger.error("guard unavailable: jev and fallback both failed (%s)", type(exc).__name__)
        return unavailable(model=model)
    return decide(scores, source="fallback", model=result.served_by or result.model, policy=policy)


async def check_intent(intent: str, *, policy: GuardPolicy = DEFAULT_POLICY) -> GuardDecision:
    """Classify one intent. Never raises for a classifier outage — that is the
    ``unavailable`` verdict — but lets ``SpendCapReached`` through so the
    caller can pause planning with its own notice."""
    state = classifier_state(intent)
    if not state:
        return GuardDecision(verdict="needs_detail", reasons=["unclear"], message=prompts.NEEDS_DETAIL_QUESTION)
    try:
        scores, jev_model = await _jev_scores(state)
    except SpendCapReached:
        raise
    except (JevUnavailable, MalformedAnswer) as exc:
        logger.warning("jev guard unavailable (%s); using the Claude fallback guard", type(exc).__name__)
        decision = await _fallback_decision(state, policy)
    else:
        decision = decide(scores, source="jev", model=jev_model, policy=policy)
    logger.info(
        "intent guard: verdict=%s tier=%s watch=%s source=%s reasons=%s",
        decision.verdict,
        decision.tier,
        decision.watch,
        decision.source,
        ",".join(decision.reasons),
    )
    return decision
