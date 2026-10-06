"""The typed failures of a model call.

Every caller maps these to an answer without reading a message string:

    LLMError                      base of everything below
    ├── LLMRefused                Claude declined (stop_reason "refusal"), after any server-side fallback
    ├── LLMTruncated              the reply hit max_tokens before it finished
    ├── LLMInvalidOutput          a structured reply that does not validate against its schema
    ├── LLMRequestError           Claude rejected the request itself (400/404/413/422): a bug here, not an outage
    ├── LLMUnavailable            retryable, and the retries are spent: rate limit, overload, 5xx, timeout, network
    │   └── LLMNotConfigured      no key, or a key the API refuses — unavailable until an operator acts
    ├── JevUnavailable            jev did not answer: missing key, timeout, network, or any API error
    └── SpendCapReached           today's spend has reached LLM_DAILY_SPEND_CAP_USD

`retry_after` is seconds, or None when there is nothing useful to wait for.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base of every model-layer failure."""


class LLMRefused(LLMError):
    """Claude declined the request (and so did any server-side fallback).

    `category` is the API's `stop_details.category` ("cyber", "bio",
    "frontier_llm", "reasoning_extraction", "general_harms") or None, which
    is a valid state of its own. `explanation` is informational and unstable.
    """

    def __init__(self, category: str | None, explanation: str | None, *, model: str) -> None:
        self.category = category
        self.explanation = explanation
        self.model = model
        super().__init__(f"{model} declined the request (category: {category or 'none'})")


class LLMTruncated(LLMError):
    """The reply stopped at max_tokens; its partial content is not used."""

    def __init__(self, *, model: str, max_tokens: int) -> None:
        self.model = model
        self.max_tokens = max_tokens
        super().__init__(f"{model} reached max_tokens={max_tokens} before finishing")


class LLMInvalidOutput(LLMError):
    """A structured reply that does not validate against the requested schema.

    Structured outputs constrain the shape, but constraints the API does not
    enforce (lengths, ranges) are validated here, after the call.
    """

    def __init__(self, *, model: str, schema: str, detail: str) -> None:
        self.model = model
        self.schema = schema
        self.detail = detail
        super().__init__(f"{model} returned output that is not a valid {schema}: {detail}")


class LLMRequestError(LLMError):
    """Claude rejected the request itself: a malformed request or an unknown model."""

    def __init__(self, *, status: int, error_type: str | None, model: str) -> None:
        self.status = status
        self.error_type = error_type
        self.model = model
        super().__init__(f"{model} rejected the request: HTTP {status} {error_type or ''}".rstrip())


class LLMUnavailable(LLMError):
    """Claude could not answer now; the SDK's retries are already spent.

    `reason` is one of "rate_limited", "overloaded", "server_error",
    "timeout", "connection", "billing", "missing_key", "auth".
    """

    def __init__(self, reason: str, *, retry_after: float | None = None, model: str | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        self.model = model
        super().__init__(f"Claude unavailable: {reason}" + (f" ({model})" if model else ""))


class LLMNotConfigured(LLMUnavailable):
    """No ANTHROPIC_API_KEY, or one the API refuses (401/403)."""


class JevUnavailable(LLMError):
    """jev did not answer. The guard falls back to Claude on any of these.

    `reason` is one of "missing_key", "timeout", "connection",
    "rate_limited", "server_error", "rejected", "invalid_response".
    """

    def __init__(self, reason: str, *, retry_after: float | None = None, status: int | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        self.status = status
        super().__init__(f"jev unavailable: {reason}" + (f" (HTTP {status})" if status else ""))


class SpendCapReached(LLMError):
    """Today's spend has reached the cap; AI work pauses until the UTC day turns."""

    def __init__(self, *, spent_usd: float, cap_usd: float, retry_after: int) -> None:
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.retry_after = retry_after
        super().__init__(f"daily AI spend cap reached: ${spent_usd:.4f} of ${cap_usd:.2f}")
