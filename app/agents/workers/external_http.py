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
    Endpoint  validated against the SSRF rules in `validate_endpoint_url`
              before every dispatch: https only, no private / loopback /
              link-local / reserved / multicast address literals, no loopback
              hostnames. Redirects are never followed.

Any failure — no connection after the retry, a non-2xx status, an oversize or
unreadable body, non-object JSON, or a missing `summary` — is raised as
ExternalDispatchError. execution_svc catches it exactly like a raising local
worker: the step is skipped, not billed, and the workflow degrades rather than
crashing.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import Worker

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = 1
CONNECT_TIMEOUT_SECONDS = 5.0
# Under execution_svc.STEP_TIMEOUT_SECONDS (120 s) on purpose — see module docstring.
TOTAL_TIMEOUT_SECONDS = 110.0
MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB — headroom over the ~10-60 KiB artifacts
_USER_AGENT = "orizon-orchestrator/1"
# Only https: an operator endpoint is a third party across the public internet,
# and http would put the envelope (and the artifact coming back) on the wire in
# the clear. Restricting the scheme also kills file://, gopher:// and the rest
# of the SSRF-classic schemes in the same line.
ALLOWED_SCHEMES = frozenset({"https"})
# Hostnames that resolve to the local machine without ever touching an IP
# literal. ".localhost" is reserved for exactly this by RFC 6761.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})
# Names the cloud providers resolve to the link-local metadata service. Blocking
# 169.254.169.254 as a literal does nothing about these: they are ordinary names
# that only resolve inside the VM, so nothing short of a name check stops them.
# This is a floor, not a fence — the resolve-then-check in Epic 2 is what makes
# the range unreachable by ANY name. Keep both.
_METADATA_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog", "instance-data"})


class ExternalDispatchError(RuntimeError):
    """A step dispatched to an operator endpoint did not produce a usable
    result. Raised so execution_svc treats the step as failed — identical to a
    raising local worker: skipped, unbilled, the workflow degrades."""


def _as_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` denotes, or None if it is a name.

    `ipaddress.ip_address` parses only the canonical dotted-quad, but the
    resolver is far more permissive: getaddrinfo reads "2130706433", "127.1"
    and "0177.0.0.1" as 127.0.0.1, and "2852039166" as the metadata service.
    Parsed by ip_address alone, every one of those spellings falls through to
    the hostname rules — which only know `localhost` — and is then handed to
    the connect as the blocked address after all.

    socket.inet_aton IS the resolver's own parser, so it recognises exactly the
    spellings getaddrinfo would go on to honour: decimal, octal, hex and the
    short a.b / a.b.c forms. A name that inet_aton accepts is all-numeric and
    therefore cannot be a public FQDN, so nothing legitimate is caught here.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError):
        return None  # a real name, not a literal in disguise


def validate_endpoint_url(url: str) -> None:
    """Reject an operator endpoint that is not safe to dispatch to (SSRF).

    An operator-supplied URL is an outbound request made by *our* server from
    *inside* our network, so an unchecked one turns the orchestrator into a
    proxy for anything the endpoint can reach: cloud instance metadata at
    169.254.169.254 (credentials), 127.0.0.1 (this process' own admin surface),
    and the private ranges holding the database and the internal services.

    The rules, in order:
      * scheme must be https — see ALLOWED_SCHEMES;
      * a host must be present;
      * an IP literal must be publicly routable — private, loopback,
        link-local (which is what 169.254.169.254 is), reserved, multicast and
        unspecified addresses are all refused, v4 and v6 alike. "IP literal"
        means any spelling the RESOLVER treats as one, not just the canonical
        dotted-quad — see _as_ip_literal;
      * a hostname must not be a loopback name (`localhost`, `*.localhost`)
        or a known cloud metadata name (`metadata.google.internal`, …).

    Raises ExternalDispatchError so a bad binding fails its step like any other
    dispatch failure rather than crashing the run loop.

    Deliberately NOT covered: this validates the URL as written, so an ordinary
    name that RESOLVES into a blocked range still gets through — the metadata
    names above are a hand-listed floor, not a general answer, and DNS
    rebinding between this check and the connect is untouched. Closing that
    needs resolve-then-pin at socket level, which belongs with operator
    endpoint binding in Epic 2. Redirects cannot launder the check because we
    never follow them (see _dispatch).
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as e:  # malformed IPv6 bracket, non-numeric port, …
        raise ExternalDispatchError(f"endpoint URL {url!r} could not be parsed") from e

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ExternalDispatchError(
            f"endpoint URL {url!r} uses scheme {scheme or '(none)'!r}; only {sorted(ALLOWED_SCHEMES)} allowed"
        )

    host = (host or "").rstrip(".")  # a trailing-dot FQDN names the same host
    if not host:
        raise ExternalDispatchError(f"endpoint URL {url!r} has no host")

    ip = _as_ip_literal(host)
    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            # Report the canonical form: "2130706433" is not obviously 127.0.0.1
            # in a log line, and the operator needs to see what we resolved it to.
            raise ExternalDispatchError(
                f"endpoint URL {url!r} points at non-public address {ip} — "
                "private, loopback, link-local, reserved, multicast and unspecified "
                "ranges are not dispatchable"
            )
        return

    if host in _LOOPBACK_HOSTNAMES or any(host.endswith(f".{name}") for name in _LOOPBACK_HOSTNAMES):
        raise ExternalDispatchError(f"endpoint URL {url!r} points at loopback host {host!r}")

    if host in _METADATA_HOSTNAMES:
        raise ExternalDispatchError(f"endpoint URL {url!r} points at cloud metadata host {host!r}")


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
        }
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": dispatch_id,
            "User-Agent": _USER_AGENT,
        }
        return await self._dispatch(payload, headers, dispatch_id)

    async def _dispatch(self, payload: dict[str, Any], headers: dict[str, str], dispatch_id: str) -> dict[str, Any]:
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
            attempts = 0
            while True:
                attempts += 1
                try:
                    return await self._once(client, payload, headers, dispatch_id)
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
        self, client: httpx.AsyncClient, payload: dict[str, Any], headers: dict[str, str], dispatch_id: str
    ) -> dict[str, Any]:
        async with client.stream("POST", self.endpoint_url, json=payload, headers=headers) as resp:
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
