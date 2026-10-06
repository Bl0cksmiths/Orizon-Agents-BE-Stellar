"""One Claude call for one worker step, with the step-failure contract.

Every built-in LLM worker reaches Claude through here, so a refusal, a
truncated answer, an exhausted retry budget and the daily spend cap all leave a
worker the same way: as a `ModelStepError` carrying a failure class. The run
loop (`execution_svc`) already treats any exception from `worker.run` as that
step's failure — skipped, NOT added to the run's spend, so neither the
simulated total nor the on-chain settle charges for it, and the run carries on
— and reads the class off `exc.rule` for the trace and the failure streak.

The classes are lowercase tokens in the run loop's closed vocabulary
(`_FAILURE_CLASS_RE`). They never carry the model's words: a refusal's
explanation goes to the server log, never to the world-readable trace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from ...llm.errors import (
    LLMError,
    LLMInvalidOutput,
    LLMNotConfigured,
    LLMRefused,
    LLMTruncated,
    LLMUnavailable,
    SpendCapReached,
)
from ...llm.tiers import display_name, effort_for, model_for

if TYPE_CHECKING:
    from ...llm.tiers import Tier

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Failure classes a model step can end in.
MODEL_REFUSED = "model_refused"
MODEL_TRUNCATED = "model_truncated"
MODEL_UNAVAILABLE = "model_unavailable"
MODEL_NOT_CONFIGURED = "model_not_configured"
SPEND_CAP_REACHED = "spend_cap_reached"
MODEL_ERROR = "model_error"
INVALID_OUTPUT = "invalid_output"


class ModelStepError(Exception):
    """A worker step that a model did not deliver. `rule` is its failure class."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


def _step_error(worker: str, model: str, exc: LLMError) -> ModelStepError:
    if isinstance(exc, LLMRefused):
        # The category is a fixed API token (cyber, bio, …); the explanation is
        # the model's prose and stays in this log line only.
        logger.warning("%s: %s declined the step (category %s): %s", worker, model, exc.category, exc.explanation)
        return ModelStepError(MODEL_REFUSED, f"{worker}: {model} declined (category {exc.category})")
    if isinstance(exc, LLMTruncated):
        return ModelStepError(MODEL_TRUNCATED, f"{worker}: {model} hit max_tokens before finishing")
    if isinstance(exc, LLMInvalidOutput):
        return ModelStepError(INVALID_OUTPUT, f"{worker}: {model} returned output outside its schema")
    if isinstance(exc, SpendCapReached):
        return ModelStepError(SPEND_CAP_REACHED, f"{worker}: daily model spend cap reached")
    if isinstance(exc, LLMNotConfigured):
        return ModelStepError(MODEL_NOT_CONFIGURED, f"{worker}: no usable Anthropic API key")
    if isinstance(exc, LLMUnavailable):
        return ModelStepError(MODEL_UNAVAILABLE, f"{worker}: {model} unavailable ({exc.reason})")
    return ModelStepError(MODEL_ERROR, f"{worker}: {model} call failed: {type(exc).__name__}")


def _note_fallback(worker: str, model: str, served_by: str | None) -> None:
    """Log a step a server-side fallback answered. The call itself (tokens,
    cost, latency) is already logged once by `app/llm/claude.py`."""
    if served_by and served_by != model:
        logger.info("%s: %s declined, answered by fallback %s", worker, display_name(model), display_name(served_by))


async def structured(
    *,
    worker: str,
    tier: Tier,
    system: str,
    user: str,
    schema: type[T],
    max_tokens: int,
) -> T:
    """Ask the tier's model for one `schema` object (structured output).

    The tier's effort is always passed; `app/llm/claude.py` leaves it off for a
    model that takes none (Claude Haiku 4.5).
    """
    from ...llm import claude

    model = model_for(tier)
    try:
        result = await claude.structured(
            purpose=f"worker.{worker}",
            model=model,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            effort=effort_for(tier),
        )
    except LLMError as e:
        raise _step_error(worker, model, e) from e
    _note_fallback(worker, model, result.served_by)
    return result.value


async def text(
    *,
    worker: str,
    tier: Tier,
    system: str,
    user: str,
    max_tokens: int,
) -> str:
    """Ask the tier's model for a long text answer, streamed.

    Streamed because the answer is a whole HTML app: a request that size must
    stream (the SDK refuses a non-streamed one it expects to outlast its idle
    timeout), and the final message is read once the stream completes.
    """
    from ...llm import claude

    model = model_for(tier)
    try:
        result = await claude.text(
            purpose=f"worker.{worker}",
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            effort=effort_for(tier),
            stream=True,
        )
    except LLMError as e:
        raise _step_error(worker, model, e) from e
    _note_fallback(worker, model, result.served_by)
    return result.value
