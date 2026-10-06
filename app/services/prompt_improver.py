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

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

from app.agents.workers.prompt_safety import sanitize_untrusted
from app.llm.errors import LLMError
from app.llm.tiers import Effort, Tier

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
