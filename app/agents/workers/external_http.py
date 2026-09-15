"""External-agent execution over HTTP — the story-1.06 dispatch prototype.

Candidate A of the 1.06 spike (off-chain endpoint binding): an externally
registered agent is executed by POSTing its step to an operator-hosted URL and
mapping the JSON response back into the worker-output dict the orchestrator
already consumes. This implements the existing `Worker` interface, so it drops
straight into `execution_svc._run` at the `get_worker` seam — the exact point
where an unknown agent is skipped today — with no change to the run loop.

Envelope (frozen by this spike; see docs/decisions/0001-external-agent-execution.md):

    Request   POST {endpoint}
              headers  Content-Type: application/json
                       Idempotency-Key: {dispatch_id}
                       User-Agent: orizon-orchestrator/1
              body     {"v": 1, "agent_id", "intent", "rationale",
                        "context", "dispatch_id"}

    Response  200, application/json, body = the worker-output object:
              {"summary": str (required, non-empty),
               "artifact"?, "critic_violations"?, "critic_notes"?,
               "preview_url"?, "source"?}

    Timeouts  connect 5 s, total 110 s — under execution_svc.STEP_TIMEOUT_SECONDS
              (120 s) so a slow operator is judged here, cleanly, as a failed
              step rather than as the run loop's ambiguous outer timeout.
    Retry     at most once, and ONLY when the connection never established
              (ConnectError / ConnectTimeout): the operator never received the
              step, so a retry cannot double-run committed work, and the
              unchanged Idempotency-Key lets it dedupe anyway. A returned status
              — even 5xx — is never retried: the operator answered.
    Size cap  response body streamed and capped at MAX_RESPONSE_BYTES; an
              oversize body fails the step before it is buffered.
    Endpoint  validated before every dispatch against the SSRF rules, which now
              live in `app.services.endpoint_policy` because the bind API needs
              the same ones: https only, no private / loopback / link-local /
              reserved / multicast address literals, no loopback or cloud
              metadata hostnames. Redirects are never followed.

Any failure — no connection after the retry, a non-2xx status, an oversize or
unreadable body, non-object JSON, or a missing `summary` — is raised as
ExternalDispatchError. execution_svc catches it exactly like a raising local
worker: the step is skipped, not billed, and the workflow degrades rather than
crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

import httpx

from app.config import settings
from app.services.dispatch_signing import sign_dispatch
from app.services.endpoint_policy import EndpointPolicyError
from app.services.endpoint_policy import validate_endpoint_url as _validate_endpoint_policy

from .base import Worker

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = 2
CONNECT_TIMEOUT_SECONDS = 5.0
# Under execution_svc.STEP_TIMEOUT_SECONDS (120 s) on purpose — see module docstring.
TOTAL_TIMEOUT_SECONDS = 110.0
MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB — headroom over the ~10-60 KiB artifacts
# The REAL ceiling on one dispatch, measured on a monotonic clock across connect
# + stream + parse. httpx has no total-request timeout: httpx.Timeout(110.0,
# connect=5.0) resolves to read/write/pool=110, each of which is only an IDLE GAP
# between reads. An operator trickling one byte every 109 s therefore never trips
# it and runs until execution_svc's 120 s step ceiling — exactly the "ambiguous
# outer timeout" this module's docstring claims cannot happen. Enforced here so
# the promise is true, with headroom left for _settle_onchain afterwards.
DISPATCH_DEADLINE_SECONDS = 100.0
_USER_AGENT = "orizon-orchestrator/1"


class ExternalDispatchError(RuntimeError):
    """A step dispatched to an operator endpoint did not produce a usable
    result. Raised so execution_svc treats the step as failed — identical to a
    raising local worker: skipped, unbilled, the workflow degrades."""


def validate_endpoint_url(url: str) -> None:
    """Reject an operator endpoint that is not safe to dispatch to (SSRF).

    The rules themselves are `app.services.endpoint_policy.validate_endpoint_url`
    — one block-list, shared with the bind API, because a second copy is a copy
    that drifts. This wrapper exists only to keep the dispatch path's error type:
    `EndpointPolicyError` becomes `ExternalDispatchError`, so a bad binding fails
    its step like any other dispatch failure rather than surfacing a ValueError
    the run loop has no handling for. The message is passed through unchanged.
    """
    try:
        _validate_endpoint_policy(url)
    except EndpointPolicyError as e:
        raise ExternalDispatchError(str(e)) from e


class ExternalHttpWorker(Worker):
    """Dispatches a plan step to an operator-hosted HTTP endpoint.

    `client` is injectable so a test can drive an in-process ASGI endpoint via
    httpx.ASGITransport; in production the worker owns a short-lived client per
    dispatch, configured with the envelope timeouts.
    """

    real = True

    def __init__(
        self,
        agent_id: str,
        name: str,
        endpoint_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = agent_id
        self.name = name
        self.endpoint_url = endpoint_url
        self._client = client

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dispatch_id = secrets.token_hex(8)
        payload: dict[str, Any] = {
            "v": ENVELOPE_VERSION,
            "agent_id": self.id,
            "intent": intent,
            "rationale": rationale,
            "context": context or {},
            "dispatch_id": dispatch_id,
            # Freshness and deployment, both inside the signed bytes. SEP-53's
            # preimage carries no network id — unlike Stellar transaction
            # signing — so without `network` a testnet dispatch is byte-identical
            # in framing to a mainnet one and could be replayed across them.
            "ts": int(time.time()),
            "network": settings.stellar_network,
        }
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": dispatch_id,
            "User-Agent": _USER_AGENT,
        }
        # Serialized ONCE, here, and those exact bytes are what we sign and what
        # we send. httpx's `json=` encodes with separators=(",", ":") and
        # ensure_ascii=False, which differs from json.dumps() defaults — so
        # signing json.dumps(payload) while sending json=payload would sign
        # bytes the operator never receives. That breaks only when the payload
        # contains non-ASCII, i.e. it passes every ASCII test and fails in
        # production on the first accented character.
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        # Signed over the destination URL AND these bytes, so a signature
        # captured by one operator cannot be replayed at another — the verifier
        # has to supply their own endpoint URL to rebuild the message. Empty
        # when no dispatch key is configured: an unsigned dispatch is a trust
        # gap the OPERATOR is positioned to enforce (they can reject it), not a
        # reason for us to fail a step.
        headers.update(sign_dispatch(self.endpoint_url, body))
        return await self._dispatch(body, headers, dispatch_id)

    async def _dispatch(self, body: bytes, headers: dict[str, str], dispatch_id: str) -> dict[str, Any]:
        # Validated HERE, per dispatch, rather than in __init__, for two reasons.
        # (1) Blast radius: execution_svc._run calls get_worker OUTSIDE its
        #     per-step try/except, so a worker that raised at construction would
        #     take down the whole workflow; raising on the dispatch path fails
        #     just this step, unbilled, like every other ExternalDispatchError.
        # (2) Coverage: endpoint_url is a plain attribute, so a URL rebound
        #     after construction (an operator re-binding, a mutated registry
        #     row) is re-checked on every attempt instead of trusting a
        #     one-time check from whenever the worker happened to be built.
        try:
            validate_endpoint_url(self.endpoint_url)
        except ExternalDispatchError as e:
            # Re-raised with the module's prefix so the refusal correlates with
            # the rest of this dispatch's log lines.
            raise ExternalDispatchError(f"external dispatch {dispatch_id} to {self.id}: {e}") from e

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            # httpx does not follow redirects by default; spelled out because the
            # validation above only covers the URL WE dispatch to. A followed 30x
            # would let an operator bounce us to 169.254.169.254 unchecked, so
            # this must stay False.
            follow_redirects=False,
        )
        try:
            # One deadline for the whole dispatch, retry included — a monotonic
            # clock so a wall-clock adjustment mid-dispatch cannot extend it.
            deadline = time.monotonic() + DISPATCH_DEADLINE_SECONDS
            attempts = 0
            while True:
                attempts += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExternalDispatchError(
                        f"external dispatch {dispatch_id} to {self.id}: "
                        f"exceeded the {DISPATCH_DEADLINE_SECONDS:.0f}s dispatch deadline"
                    )
                try:
                    return await asyncio.wait_for(self._once(client, body, headers, dispatch_id), timeout=remaining)
                except asyncio.TimeoutError as e:
                    # A slow-but-alive operator: judged HERE, as a failed step,
                    # rather than by execution_svc's outer ceiling. Never
                    # retried — the request was on the wire and may have run.
                    raise ExternalDispatchError(
                        f"external dispatch {dispatch_id} to {self.id}: "
                        f"no response within {DISPATCH_DEADLINE_SECONDS:.0f}s"
                    ) from e
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    # The connection never established, so the operator never
                    # received the step: a single retry cannot double-run work.
                    if attempts >= 2:
                        raise ExternalDispatchError(
                            f"external dispatch {dispatch_id} to {self.id}: no connection after retry"
                        ) from e
                    logger.warning(
                        "external dispatch %s to %s: connection failed (%s) — retrying once",
                        dispatch_id,
                        self.id,
                        type(e).__name__,
                    )
        finally:
            if owns_client:
                await client.aclose()

    async def _once(
        self, client: httpx.AsyncClient, body: bytes, headers: dict[str, str], dispatch_id: str
    ) -> dict[str, Any]:
        # `content=` not `json=`: these are the bytes the signature covers.
        async with client.stream("POST", self.endpoint_url, content=body, headers=headers) as resp:
            if resp.status_code // 100 != 2:
                raise ExternalDispatchError(f"external dispatch {dispatch_id} to {self.id}: HTTP {resp.status_code}")
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ExternalDispatchError(
                        f"external dispatch {dispatch_id} to {self.id}: response exceeds {MAX_RESPONSE_BYTES}-byte cap"
                    )
                chunks.append(chunk)
        return self._parse(b"".join(chunks), dispatch_id)

    def _parse(self, body: bytes, dispatch_id: str) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ExternalDispatchError(
                f"external dispatch {dispatch_id} to {self.id}: response was not valid JSON"
            ) from e
        if not isinstance(data, dict):
            raise ExternalDispatchError(
                f"external dispatch {dispatch_id} to {self.id}: response JSON was "
                f"{type(data).__name__}, expected an object"
            )
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ExternalDispatchError(
                f"external dispatch {dispatch_id} to {self.id}: response missing a non-empty 'summary'"
            )
        return data
