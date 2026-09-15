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

    Deadline  DISPATCH_DEADLINE_SECONDS (100 s), measured on a monotonic clock
              across connect + stream + parse and covering the retry, so a slow
              operator is judged HERE as a failed step rather than by the run
              loop's ambiguous outer ceiling (STEP_TIMEOUT_SECONDS, 120 s), with
              headroom left for settlement afterwards.
              The httpx timeouts below it are a floor, not the ceiling: httpx
              has NO total-request timeout, and its `read` value is only the
              idle gap between reads — an operator trickling one byte at a time
              satisfies it forever. Story 2.02 fixed that; before it, this
              docstring described a guarantee the code did not provide.
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
    Failure   every failure leaves this module as an ExternalDispatchError
              carrying a `rule` from DISPATCH_RULES: endpoint_refused,
              no_connection, response_timeout, transport_error, error_status,
              oversize_response, invalid_response. Before story 2.03 the
              sentence here claimed that unconditionally and the code did not
              deliver it — `_dispatch` caught asyncio.TimeoutError,
              ConnectError and ConnectTimeout and NOTHING else, so
              RemoteProtocolError, DecodingError, ReadTimeout, ReadError,
              PoolTimeout, WriteTimeout and TooManyRedirects came out as raw
              httpx errors, which execution_svc cannot classify and no
              per-agent failure count can see. Twelve failure modes also
              rendered to the buyer as one trace line; the rule is what makes
              them three different lines and a groupable log.
    Refusals  name the HOST and the rule, never the URL — ADR 0003's logging
              rule, which the dispatch path now follows too. An operator
              endpoint can carry a credential in its query string, and an
              ExternalDispatchError message is logged verbatim upstream.

execution_svc catches ExternalDispatchError exactly like a raising local
worker: the step is skipped, not billed, and the workflow degrades rather than
crashing. It reads the class duck-typed — `getattr(e, "rule", None)` — so the
run loop never imports a worker's module to classify a failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.agents.workers.external_contract import ExternalOutputError, parse_operator_output
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


# The closed vocabulary of dispatch failure classes. Closed on purpose:
# ExternalDispatchError rejects anything outside it, so a typo at a raise site
# fails loudly here rather than travelling to the trace as a class nothing
# handles. Same shape, deliberately, as `endpoint_policy.ENDPOINT_RULES` and
# `external_contract.OUTPUT_RULES`: three vocabularies that behaved differently
# would be three things to learn.
#
# COARSE on purpose. A dispatch has a dozen distinct ways to fail, but a class
# earns an entry here only if an operator would do something DIFFERENT about
# it, so each one below is one remedy: rebind the endpoint, come up, answer
# faster, fix the HTTP stack, stop answering non-2xx, send less, send the
# documented shape. The specific cause — a wrapped `EndpointPolicyError.rule`
# or `ExternalOutputError.rule`, or the httpx exception's own type name — stays
# in the prose, where whoever debugs one dispatch will look; the class is what
# groups a week of them.
DISPATCH_RULES: frozenset[str] = frozenset(
    {
        # The URL failed the SSRF policy, so nothing was dispatched at all.
        "endpoint_refused",
        # No connection was ever established, retry included: the operator
        # never received the step.
        "no_connection",
        # Time ran out: the request was committed to the wire (or is still
        # waiting on a connection from our own pool) and no usable response
        # came back inside the dispatch deadline. Never retried.
        "response_timeout",
        # The connection existed and the HTTP conversation itself broke.
        "transport_error",
        # The operator answered, with something that is not a usable 2xx.
        "error_status",
        # The response body exceeded the size cap and was cut off unread.
        "oversize_response",
        # The body arrived whole and is not the documented shape.
        "invalid_response",
    }
)


class ExternalDispatchError(RuntimeError):
    """A step dispatched to an operator endpoint did not produce a usable
    result. Raised so execution_svc treats the step as failed — identical to a
    raising local worker: skipped, unbilled, the workflow degrades.

    `rule` is one of `DISPATCH_RULES`; the constructor refuses any other value
    so the vocabulary cannot quietly grow a synonym. The rule — not the prose —
    is the machine-readable part: it is what the buyer's trace line names, what
    an operator log groups on and what a test asserts on, which leaves the
    message free to say something useful to a human without a test pinning its
    wording.

    RuntimeError, not the ValueError its two sibling vocabularies use: this is
    not bad input to a function, it is a remote party failing to hold up its
    end, and execution_svc's per-step handler already catches it as such.
    """

    def __init__(self, rule: str, message: str) -> None:
        if rule not in DISPATCH_RULES:
            raise ValueError(f"unknown dispatch rule {rule!r}")
        super().__init__(message)
        self.rule = rule


def _refusal_host(url: str) -> str:
    """The host of `url`, for a line someone will read in a log.

    `app.routers.binding._host` for the dispatch path, and deliberately the same
    shape: a refusal names the host, never the URL.
    """
    try:
        return urlsplit(url).hostname or "-"
    except ValueError:  # malformed IPv6 bracket, non-numeric port, …
        return "-"


def _without_url(url: str, message: str) -> str:
    """`message` with the endpoint URL cut down to its host.

    Every `EndpointPolicyError` message embeds `{url!r}`, which is right there —
    the bind API answers the operator who typed the URL, so quoting it back is
    the useful thing to do. A dispatch refusal has a different audience: it
    becomes the `ExternalDispatchError` that `execution_svc` logs verbatim, so
    the same prose writes the WHOLE endpoint into our server log, credentials
    and all. `https://op.example/dispatch?token=SECRET` is an ordinary way to
    write an operator endpoint and the token belongs in no log line of ours.

    So the URL comes out and the host goes in, which is exactly what the bind
    path's refusals carry (ADR 0003: host and rule, never the URL). The prose
    around it survives — "points at non-public address 169.254.169.254" is the
    part that explains the refusal, and it names nothing secret.

    Belt and braces: if the URL is still in the message afterwards — prose that
    quoted it in some shape this does not know how to redact — the prose is
    dropped rather than the guarantee.
    """
    host = _refusal_host(url)
    safe = message.replace(repr(url), f"for host {host!r}")
    try:
        query = urlsplit(url).query
    except ValueError:
        query = ""
    if (url and url in safe) or (query and query in safe):
        return f"endpoint for host {host!r} refused"
    return safe


def validate_endpoint_url(url: str) -> None:
    """Reject an operator endpoint that is not safe to dispatch to (SSRF).

    The rules themselves are `app.services.endpoint_policy.validate_endpoint_url`
    — one block-list, shared with the bind API, because a second copy is a copy
    that drifts. This wrapper exists to keep the dispatch path's error type:
    `EndpointPolicyError` becomes `ExternalDispatchError`, so a bad binding fails
    its step like any other dispatch failure rather than surfacing a ValueError
    the run loop has no handling for.

    The message is rewritten on the way through, not passed along: the URL is
    redacted to its host (see `_without_url`) and the policy's own `rule` is
    appended, so the coarse class reaches the trace while the specific reason
    the endpoint was refused stays readable in the log.
    """
    try:
        _validate_endpoint_policy(url)
    except EndpointPolicyError as e:
        raise ExternalDispatchError("endpoint_refused", f"{_without_url(url, str(e))} (rule: {e.rule})") from e


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
            # Re-raised with the module's prefix so the refusal correlates
            # with the rest of this dispatch's log lines, and with the wrapped
            # class carried through: a refusal is still an endpoint refusal
            # once this dispatch's prefix is on the front of it.
            raise ExternalDispatchError(e.rule, f"external dispatch {dispatch_id} to {self.id}: {e}") from e

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
                        "response_timeout",
                        f"external dispatch {dispatch_id} to {self.id}: "
                        f"exceeded the {DISPATCH_DEADLINE_SECONDS:.0f}s dispatch deadline",
                    )
                try:
                    return await asyncio.wait_for(self._once(client, body, headers, dispatch_id), timeout=remaining)
                except asyncio.TimeoutError as e:
                    # A slow-but-alive operator: judged HERE, as a failed step,
                    # rather than by execution_svc's outer ceiling. Never
                    # retried — the request was on the wire and may have run.
                    raise ExternalDispatchError(
                        "response_timeout",
                        f"external dispatch {dispatch_id} to {self.id}: "
                        f"no response within {DISPATCH_DEADLINE_SECONDS:.0f}s",
                    ) from e
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    # The connection never established, so the operator never
                    # received the step: a single retry cannot double-run work.
                    if attempts >= 2:
                        raise ExternalDispatchError(
                            "no_connection",
                            f"external dispatch {dispatch_id} to {self.id}: "
                            f"no connection after retry ({type(e).__name__})",
                        ) from e
                    logger.warning(
                        "external dispatch %s to %s: connection failed (%s) — retrying once",
                        dispatch_id,
                        self.id,
                        type(e).__name__,
                    )
                except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                    # The other three httpx timeouts, none of which the clause
                    # above catches (ConnectTimeout is a sibling, not a base).
                    # None is retried, and only ConnectTimeout ever could be:
                    # a read timeout means the request reached the operator and
                    # may be running right now, and a write timeout means part
                    # of it did — retrying either is exactly the double-run the
                    # module's "no retry on a step that may have executed" rule
                    # exists to prevent. PoolTimeout never left this process at
                    # all, so a retry would be SAFE, but it would queue for the
                    # same exhausted pool inside the same deadline; it is
                    # grouped here because what an operator sees is a dispatch
                    # that ran out of time.
                    raise ExternalDispatchError(
                        "response_timeout",
                        f"external dispatch {dispatch_id} to {self.id}: no usable response ({type(e).__name__})",
                    ) from e
                except httpx.TooManyRedirects as e:
                    # Unreachable while follow_redirects stays False — a 30x is
                    # a non-2xx and fails as `error_status` in _once — but an
                    # injected client can be built with redirects on, so it is
                    # classed the same way: the operator answered, with a chain
                    # of statuses we will not use.
                    raise ExternalDispatchError(
                        "error_status",
                        f"external dispatch {dispatch_id} to {self.id}: too many redirects ({type(e).__name__})",
                    ) from e
                except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as e:
                    # EVERYTHING else httpx can raise, so the docstring's "any
                    # failure is raised as ExternalDispatchError" is true rather
                    # than aspirational: RemoteProtocolError (the operator spoke
                    # malformed HTTP or hung up mid-body), DecodingError (their
                    # Content-Encoding does not match their bytes), ReadError /
                    # WriteError / CloseError, ProxyError, UnsupportedProtocol,
                    # LocalProtocolError. Before this clause each of those left
                    # the module as a raw httpx error, which execution_svc
                    # cannot classify and the failure tracker cannot count.
                    #
                    # Not retried: RemoteProtocolError and ReadError both follow
                    # a request that was fully sent, so the step may have run.
                    #
                    # The httpx TYPE goes in the message, never its str(): an
                    # httpx error's text can quote the request URL, which is the
                    # one thing no message leaving this module may carry.
                    raise ExternalDispatchError(
                        "transport_error",
                        f"external dispatch {dispatch_id} to {self.id}: transport failure ({type(e).__name__})",
                    ) from e
        finally:
            if owns_client:
                await client.aclose()

    async def _once(
        self, client: httpx.AsyncClient, body: bytes, headers: dict[str, str], dispatch_id: str
    ) -> dict[str, Any]:
        # `content=` not `json=`: these are the bytes the signature covers.
        async with client.stream("POST", self.endpoint_url, content=body, headers=headers) as resp:
            if resp.status_code // 100 != 2:
                raise ExternalDispatchError(
                    "error_status", f"external dispatch {dispatch_id} to {self.id}: HTTP {resp.status_code}"
                )
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ExternalDispatchError(
                        "oversize_response",
                        f"external dispatch {dispatch_id} to {self.id}: response exceeds {MAX_RESPONSE_BYTES}-byte cap",
                    )
                chunks.append(chunk)
        return self._parse(b"".join(chunks), dispatch_id)

    def _parse(self, body: bytes, dispatch_id: str) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ExternalDispatchError(
                "invalid_response", f"external dispatch {dispatch_id} to {self.id}: response was not valid JSON"
            ) from e
        except RecursionError as e:
            # ~1 MiB of "[[[[..." is ~500k deep and blows CPython's recursive
            # scanner. RecursionError subclasses RuntimeError, so neither the
            # tuple above nor a ValueError catch holds it — without this it
            # escapes as a RUN failure instead of a step failure, taking the
            # whole workflow (and everyone else's settlement) with it.
            raise ExternalDispatchError(
                "invalid_response", f"external dispatch {dispatch_id} to {self.id}: response nesting too deep"
            ) from e

        # Everything past here is the operator's shape, not ours: allowlisted,
        # type-checked and clamped into a NEW dict by the contract, so no
        # operator-chosen key ever reaches context, the trace, or a later
        # operator. See ADR 0004 D1.
        try:
            output = parse_operator_output(data)
        except ExternalOutputError as e:
            raise ExternalDispatchError(
                "invalid_response", f"external dispatch {dispatch_id} to {self.id}: {e} (rule: {e.rule})"
            ) from e

        # Provenance is stamped by US, never claimed by the operator. The
        # contract drops any `source` they send, because synthetic_rating awards
        # 95/100 for source == "baked" — a one-word self-award. This value is
        # our assertion about where the output came from, so it means something
        # theirs never could.
        output["source"] = "external"
        return output
