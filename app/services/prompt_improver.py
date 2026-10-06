"""Prompt improver: turn a raw intent into a structured ``Spec``, then re-check it.

1. ``improve(intent, tier)`` — Claude Sonnet 5.5 rewrites the fenced intent as
   a ``Spec`` {goal, deliverable, constraints, done_criteria, summary} with
   structured output. The model is told to keep the user's meaning and never
   add capabilities, agents, payments, tools or URLs; as a deterministic
   backstop any link the original did not contain is scrubbed from the spec.
2. ``recheck(original, spec)`` — jev answers ``same_request`` about original +
   spec together, and injection/harmful/severity about the spec ALONE, in two
   concurrent calls. jev down → the same questions on Claude Haiku 4.5.
3. ``resolve(decision, check)`` — combines the guard's decision with the
   re-check into what the planner should do: plan from the spec, plan from
   the (fenced) original, block, ask for detail, or report unavailable.

``Spec`` doubles as the API model for a user-edited "We understood this as…"
panel, so its fields are bounded like any other request input.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.workers.prompt_safety import fence_untrusted, sanitize_untrusted
from app.llm import claude, jev
from app.llm.errors import JevUnavailable, LLMError, LLMRefused, SpendCapReached
from app.llm.tiers import Effort, Tier, guard_fallback_model, improver_model

from . import intent_guard_prompts as guard_prompts
from . import prompt_improver_prompts as prompts
from .intent_guard import (
    DEFAULT_POLICY,
    GuardPolicy,
    MalformedAnswer,
    answer,
    classifier_state,
    level,
    unit,
)

logger = logging.getLogger(__name__)

MAX_GOAL_CHARS = 300
MAX_DELIVERABLE_CHARS = 300
MAX_ITEM_CHARS = 200
MAX_ITEMS = 8
MAX_SUMMARY_CHARS = 300

# Sonnet 5.5 thinks adaptively; this covers thinking plus a bounded spec.
_IMPROVER_MAX_TOKENS = 4000
_FALLBACK_MAX_TOKENS = 300

# Spec writing is light work: a complex request needs a careful read, not deep
# reasoning, so effort tops out at medium.
_IMPROVER_EFFORT: dict[Tier, Effort] = {"low": "low", "moderate": "medium", "complex": "medium"}

_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
LINK_REMOVED = "[link removed]"


class Spec(BaseModel):
    """What the planner builds from. Bounded so a user-edited spec is a safe input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    goal: str = Field(..., min_length=1, max_length=MAX_GOAL_CHARS)
    deliverable: str = Field(..., min_length=1, max_length=MAX_DELIVERABLE_CHARS)
    constraints: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    done_criteria: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    summary: str = Field(..., min_length=1, max_length=MAX_SUMMARY_CHARS)


class SpecDraft(BaseModel):
    """The structured-output schema. Unbounded on purpose: structured outputs
    cannot enforce lengths, so ``normalize_spec`` clamps instead of letting a
    long-but-good answer fail validation."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    deliverable: str
    constraints: list[str]
    done_criteria: list[str]
    summary: str


class ImproverOutputInvalid(LLMError):
    """The model returned a spec with nothing usable in a required field."""


def _clean(text: str, limit: int, original: str) -> str:
    cleaned = " ".join(sanitize_untrusted(text).split())
    cleaned = _URL.sub(lambda m: m.group(0) if m.group(0) in original else LINK_REMOVED, cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _clean_items(items: list[str], original: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = _clean(item, MAX_ITEM_CHARS, original)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out[:MAX_ITEMS]


def normalize_spec(draft: SpecDraft, original: str) -> Spec:
    """Clamp, de-duplicate and de-link a model draft into a valid ``Spec``."""
    goal = _clean(draft.goal, MAX_GOAL_CHARS, original)
    deliverable = _clean(draft.deliverable, MAX_DELIVERABLE_CHARS, original)
    summary = _clean(draft.summary, MAX_SUMMARY_CHARS, original)
    if not goal or not deliverable or not summary:
        raise ImproverOutputInvalid("the improver returned an empty goal, deliverable or summary")
    return Spec(
        goal=goal,
        deliverable=deliverable,
        constraints=_clean_items(draft.constraints, original),
        done_criteria=_clean_items(draft.done_criteria, original),
        summary=summary,
    )


def spec_to_text(spec: Spec) -> str:
    """Plain rendering of a spec — what jev reads and what the planner fences."""
    lines = [f"Goal: {spec.goal}", f"Deliverable: {spec.deliverable}"]
    if spec.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {c}" for c in spec.constraints)
    if spec.done_criteria:
        lines.append("Done when:")
        lines.extend(f"- {c}" for c in spec.done_criteria)
    lines.append(f"Summary: {spec.summary}")
    return "\n".join(lines)


async def improve(intent: str, tier: Tier) -> Spec:
    """Rewrite ``intent`` as a ``Spec`` on Claude Sonnet 5.5.

    Raises the model layer's errors unchanged (``LLMRefused``,
    ``LLMUnavailable``, ``LLMTruncated``, ``SpendCapReached``) plus
    ``ImproverOutputInvalid`` (an ``LLMError``). The planner should treat any
    ``LLMError`` other than ``SpendCapReached`` as "no improved spec" and plan
    from the fenced original — the guard has already cleared it — except when
    the guard decision is ``watch``, which requires a clean spec to proceed.
    """
    result = await claude.structured(
        purpose="improve.spec",
        model=improver_model(),
        system=prompts.SYSTEM,
        user=prompts.user_prompt(intent, tier),
        schema=SpecDraft,
        max_tokens=_IMPROVER_MAX_TOKENS,
        effort=_IMPROVER_EFFORT[tier],
    )
    return normalize_spec(result.value, intent)


# --- re-check -----------------------------------------------------------------

CheckVerdict = Literal["clean", "watch", "drifted", "unsafe", "unavailable"]


class SpecCheck(BaseModel):
    """jev's (or the fallback's) reading of a spec against its original.

    ``verdict`` precedence: unsafe > drifted > watch > clean.
    """

    model_config = ConfigDict(frozen=True)

    verdict: CheckVerdict
    reasons: list[str] = []
    scores: dict[str, float] = {}
    source: Literal["jev", "fallback"] | None = None
    model: str | None = None


class RecheckAssessment(BaseModel):
    """Structured output the Claude fallback returns for the re-check."""

    model_config = ConfigDict(extra="forbid")

    same_request: float
    injection: float
    harmful: float
    severity: int


def judge_spec(
    *,
    same_request: float,
    injection: float,
    harmful: float,
    severity: float,
    source: Literal["jev", "fallback"],
    model: str | None,
    policy: GuardPolicy = DEFAULT_POLICY,
) -> SpecCheck:
    """Pure verdict for one set of re-check scores."""
    scores = {"same_request": same_request, "injection": injection, "harmful": harmful, "severity": severity}
    extra = ["fallback"] if source == "fallback" else []
    unsafe = [
        name
        for name, hit in (
            ("injection", injection >= policy.injection_block),
            ("harmful", harmful >= policy.harmful_block),
            ("severity", severity >= policy.severity_block),
        )
        if hit
    ]
    if unsafe:
        verdict: CheckVerdict = "unsafe"
        reasons = unsafe
    elif same_request < policy.same_request_min:
        verdict, reasons = "drifted", ["drifted"]
    elif injection >= policy.injection_watch:
        verdict, reasons = "watch", ["watch"]
    else:
        verdict, reasons = "clean", []
    return SpecCheck(verdict=verdict, reasons=reasons + extra, scores=scores, source=source, model=model)


def _check_unavailable(model: str | None = None) -> SpecCheck:
    return SpecCheck(verdict="unavailable", reasons=["guard_unavailable"], model=model)


async def _jev_recheck(original: str, spec_text: str, policy: GuardPolicy) -> SpecCheck:
    safety_call = jev.ask(purpose="guard.spec", state=spec_text, questions=guard_prompts.SPEC_SAFETY_BATTERY)
    same_call = jev.ask(
        purpose="guard.spec.same",
        state=guard_prompts.spec_state(original, spec_text),
        questions=guard_prompts.SAME_REQUEST_BATTERY,
    )
    # Both calls run to completion (no orphaned request), then the spend cap
    # outranks any outage so the caller can pause rather than fall back.
    safety, same = await asyncio.gather(safety_call, same_call, return_exceptions=True)
    if isinstance(safety, BaseException) or isinstance(same, BaseException):
        errors = [o for o in (safety, same) if isinstance(o, BaseException)]
        raise next((e for e in errors if isinstance(e, SpendCapReached)), errors[0])
    return judge_spec(
        same_request=unit(answer(same.answers, "same_request", "noul"), "same_request"),
        injection=unit(answer(safety.answers, "injection", "noul"), "injection"),
        harmful=unit(answer(safety.answers, "harmful", "noul"), "harmful"),
        severity=level(answer(safety.answers, "severity", "score"), "severity"),
        source="jev",
        model=safety.model,
        policy=policy,
    )


async def _fallback_recheck(original: str, spec_text: str, policy: GuardPolicy) -> SpecCheck:
    model = guard_fallback_model()
    try:
        result = await claude.structured(
            purpose="guard.spec.fallback",
            model=model,
            system=guard_prompts.RECHECK_FALLBACK_SYSTEM,
            user=fence_untrusted(guard_prompts.spec_state(original, spec_text), label="SPEC_REVIEW"),
            schema=RecheckAssessment,
            max_tokens=_FALLBACK_MAX_TOKENS,
            enforce_cap=False,  # a guard pass, like the intent guard's fallback
        )
        value: RecheckAssessment = result.value
        return judge_spec(
            same_request=unit(value.same_request, "same_request"),
            injection=unit(value.injection, "injection"),
            harmful=unit(value.harmful, "harmful"),
            severity=level(value.severity, "severity"),
            source="fallback",
            model=result.served_by or result.model,
            policy=policy,
        )
    except SpendCapReached:
        raise
    except LLMRefused:
        return SpecCheck(verdict="unsafe", reasons=["refused", "fallback"], source="fallback", model=model)
    except (LLMError, MalformedAnswer) as exc:
        logger.error("spec re-check unavailable: jev and fallback both failed (%s)", type(exc).__name__)
        return _check_unavailable(model)


async def recheck(original: str, spec: Spec, *, policy: GuardPolicy = DEFAULT_POLICY) -> SpecCheck:
    """Re-check a spec (improver-written or user-edited) against its original.

    Never raises for a classifier outage (``unavailable`` verdict); lets
    ``SpendCapReached`` through.
    """
    original_state = classifier_state(original)
    spec_text = classifier_state(spec_to_text(spec))
    try:
        check = await _jev_recheck(original_state, spec_text, policy)
    except SpendCapReached:
        raise
    except (JevUnavailable, MalformedAnswer) as exc:
        logger.warning("jev spec re-check unavailable (%s); using the Claude fallback", type(exc).__name__)
        check = await _fallback_recheck(original_state, spec_text, policy)
    logger.info("spec re-check: verdict=%s source=%s reasons=%s", check.verdict, check.source, ",".join(check.reasons))
    return check
