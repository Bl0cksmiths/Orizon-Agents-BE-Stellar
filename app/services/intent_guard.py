"""Intent guard: decide whether a console request may reach the planner.

One jev (TypeSafe) call answers a five-question battery about the raw intent —
injection, harmful + severity, real request, complexity — and the thresholds
below turn those probabilities into a verdict:

* ``block``        injection ≥ 0.70, or harmful ≥ 0.70, or severity ≥ 2
* ``needs_detail`` real_request < 0.30 (gibberish, pings, a lone keyword)
* ``allow``        everything else, with a tier; injection in [0.35, 0.70)
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

Thresholds are starting values to be tuned against the evals dataset — they
live in ``GuardPolicy`` so the evals harness can sweep them without patching.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.llm.tiers import Tier, tier_up

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
    """Decision thresholds. Defaults are the owner's starting values."""

    injection_block: float = 0.70
    injection_watch: float = 0.35
    harmful_block: float = 0.70
    severity_block: float = 2.0
    real_request_min: float = 0.30
    complexity_confidence_min: float = 0.5
    same_request_min: float = 0.5


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
