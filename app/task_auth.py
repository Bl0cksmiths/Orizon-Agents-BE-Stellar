"""
Capability-token authorization for task-scoped reads.

Execute mints a per-task read token (services/execution_svc.execute_plan)
and returns it in ExecuteResponse. When TASK_AUTH_REQUIRED is on, the
task's read routes (status, artifact, trace, stream) demand that token —
via the `X-Task-Token` header or a `token` query parameter (EventSource
cannot set headers) — or a valid operator `X-API-Key`, which bypasses
per-task tokens for ops visibility. A missing or wrong token is a 404,
never a 403, so task ids stay unenumerable. Default OFF: the public demo
stays fully open.

`TaskReadProof` is the same question asked WITHOUT the switch: "has this
caller proved they may read this task's private material?" `require_task_read`
turns a no into a 404 and only when enforcement is on; a proof turns a no into
LESS in the response, always. That is what `routers/disputes.py` withholds a
buyer's free text on — a route that must stay readable to a shared trace link,
carrying two fields that must not be.

A THIRD credential buys the free text and nothing else (D-067): the dispute
READ GRANT, `X-Dispute-Read-Grant`. The payer earns it by signing a read
challenge with the wallet the settlement names (`routers/disputes.py`'s
`read-challenge` / `read-grant` pair), so a payer who comes back in a new tab,
on another device or after a restart can read their own reason and the
platform's answer to it. It is asked by `TaskReadProof.proves_free_text` and
NOWHERE else — never by `proves`, never by `require_task_read` — so it opens
no artifact, no trace and no stream, whatever TASK_AUTH_REQUIRED says. See
`mint_read_grant` for the shape of the grant and the key behind it.

Lives outside app/security.py so the task-token concern stays separable
from the global API-key/rate-limit hardening primitives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Query

from .config import settings
from .security import header_secret_matches
from .state import state


def _matches(candidate: str, expected: str) -> bool:
    # For the TASK TOKEN only, which this module mints itself and which is
    # hex — so utf-8 and latin-1 agree on every byte of it, and a candidate
    # that is not ASCII is simply not the token. `security.header_secret_matches`
    # is the one to use for anything whose bytes came off a header and may not
    # be ASCII; it cannot be used here because `token` may arrive as a QUERY
    # parameter instead, which Starlette decodes as utf-8, not latin-1.
    return secrets.compare_digest(candidate.encode("utf-8", "ignore"), expected.encode("utf-8"))


# ── the dispute read grant (D-067) ──────────────────────────────

# How long one signature buys the free text for. An hour is a sitting: long
# enough that a payer reading a receipt, reloading it and coming back after
# lunch signs once, short enough that a grant copied out of a browser is not a
# standing credential. The contract promises `expires_at <= now + 3600`.
READ_GRANT_TTL_SECONDS = 3600

# The longest grant `mint_read_grant` can produce is well under this (a 128-char
# task id, a 56-char address and an expiry, base64url'd, plus a 43-char MAC).
# Anything longer is not one of ours and is refused before it is decoded, so the
# header cannot be used to make this module do work in proportion to its size.
MAX_READ_GRANT_CHARS = 512

_READ_GRANT_VERSION = "g1"

# The MAC key, derived with domain separation from a PER-PROCESS random root.
#
# Per-process and random, deliberately, rather than read from configuration:
#   - it needs no new secret for an operator to provision, rotate or leak;
#   - it is never STELLAR_SIGNING_KEY or API_KEY — a key that moves money or
#     admits the operator must not also be the key a read credential is
#     checked with, or a leak of either becomes a leak of both;
#   - a restart invalidating every grant costs exactly one more signature from
#     each payer who reads after it, which is the price D-067 asks to pay
#     rather than the one it asks to remove: the reason survives the restart
#     because the payer can always prove themselves again, not because a
#     credential outlived the process that minted it.
# One uvicorn worker is what this service runs (see `RateLimitMiddleware`),
# so one process holds every grant it minted. Behind several workers, a grant
# would be honoured only by the worker that minted it — the payer would be asked
# to sign again, never shown text they should not see.
#
# Derived rather than used raw so the root is never itself the MAC key of
# anything, and the derivation label names this one purpose and version: a
# second MAC in this module would derive its own key from its own label.
_READ_GRANT_KEY = hmac.new(secrets.token_bytes(32), b"orizon-dispute-read-grant:v1", hashlib.sha256).digest()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes:
    # binascii.Error is a ValueError; the caller treats either as "not a grant".
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _read_grant_mac(body: bytes) -> bytes:
    return hmac.new(_READ_GRANT_KEY, body, hashlib.sha256).digest()


def mint_read_grant(task_id: str, payer: str, *, now: float | None = None) -> tuple[str, float]:
    """A grant for `payer` to read `task_id`'s dispute free text; (grant, expires_at).

    Call this ONLY after the payer's signature over the read challenge has
    verified against the settlement's payer — the grant is the receipt for
    that proof, not a proof of its own.

    Stateless: `g1.<base64url(body)>.<base64url(HMAC-SHA256(key, body))>`,
    where the body is `task_id`, `payer` and an integer expiry, newline-joined.
    Newline, because neither a task id (the route bounds it to
    `[A-Za-z0-9_-]`) nor a G-address can hold one, so no two different triples
    can serialise to the same body. The client never needs to read it: it is
    opaque, it is echoed back in `X-Dispute-Read-Grant`, and every field in it
    is checked against the server's own records on the way back in. Nothing is
    stored, so there is nothing to evict, nothing to clean up and nothing to
    lose to the 200-task ring that lost the task token.

    The expiry is FLOORED to the second, so the `expires_at` the route reports
    is never later than the one the MAC binds.
    """
    issued = time.time() if now is None else now
    expires_at = int(issued + READ_GRANT_TTL_SECONDS)
    body = f"{task_id}\n{payer}\n{expires_at}".encode()
    grant = f"{_READ_GRANT_VERSION}.{_b64u(body)}.{_b64u(_read_grant_mac(body))}"
    return grant, float(expires_at)


def read_grant_admits(grant: str | None, task_id: str, payer: str | None, *, now: float | None = None) -> bool:
    """True when `grant` is one this process minted, unexpired, for THIS task and THIS payer.

    `payer` is the address the server's own record names — the dispute's, or
    the task's settlement's — never anything from the request. Binding the
    grant to it as well as to the task means a grant is only ever honoured for
    the party whose signature earned it, even if a task's records were ever
    rewritten under it.

    False for everything else, and never an exception: no grant, an oversized
    one, one that does not parse, a MAC that does not match (a tampered or
    foreign grant), another task's, another payer's, or an expired one. The MAC
    is compared in constant time before any field is believed.
    """
    if not grant or payer is None or len(grant) > MAX_READ_GRANT_CHARS or not grant.isascii():
        return False
    parts = grant.split(".")
    if len(parts) != 3 or parts[0] != _READ_GRANT_VERSION:
        return False
    try:
        body = _unb64u(parts[1])
        mac = _unb64u(parts[2])
    except ValueError:
        return False
    if not hmac.compare_digest(mac, _read_grant_mac(body)):
        return False
    try:
        g_task, g_payer, g_expiry = body.decode("utf-8").split("\n")
        expires_at = int(g_expiry)
    except ValueError:  # pragma: no cover - only a body this process MAC'd reaches here
        return False
    current = time.time() if now is None else now
    return (
        hmac.compare_digest(g_task.encode(), task_id.encode())
        and hmac.compare_digest(g_payer.encode(), payer.encode())
        and current < expires_at
    )


@dataclass(frozen=True)
class TaskReadProof:
    """What one request offered as proof, resolved once and asked per task.

    Two credentials, exactly the two `require_task_read` accepts, so "may this
    caller read this task?" has ONE answer in this service however it is asked.
    Frozen and inert: it holds what arrived, decides nothing at construction,
    and every decision it makes is `proves(task_id)`.

    Kept apart from `require_task_read` because the task id is not always known
    when the request is dispatched. `GET /api/disputes/{id}` is scoped to a
    task the caller never names — the dispute record does — so the credentials
    have to be carried into the handler and asked there, once the record is
    loaded.
    """

    task_token: str | None
    api_key: str | None
    # The dispute read grant (D-067). Consulted by `proves_free_text` ONLY:
    # `proves` never looks at it, so it can never stand in for a task token.
    read_grant: str | None = None

    def proves(self, task_id: str) -> bool:
        """True when this caller may read `task_id`'s private material.

        Independent of TASK_AUTH_REQUIRED, and that is the point: the switch
        decides whether a task's reads are gated at all, while this decides
        what goes into a response that is being served either way. Tying the
        two would mean the shipped default — enforcement off — published
        every buyer's words to anybody who asked.

        The operator key first, and it is checked against `settings.api_key`
        directly: an unset key proves nothing, which `header_secret_matches`
        answers for us rather than admitting everyone on a demo deployment.
        """
        if header_secret_matches(self.api_key, settings.api_key):
            return True
        expected = state.task_tokens.get(task_id)
        return expected is not None and self.task_token is not None and _matches(self.task_token, expected)

    def proves_free_text(self, task_id: str, payer: str | None) -> bool:
        """True when this caller may read the FREE TEXT on `task_id`'s disputes.

        Everything `proves` admits, plus one thing it does not: a dispute read
        grant for this task and this payer. `payer` is the one the server's
        record names. This is the ONLY place the grant is asked about, and the
        two dispute read routes are the only callers — so the grant buys the
        buyer's reason and the rejection reason, and not one byte of the
        artifact, the trace or the stream.
        """
        return self.proves(task_id) or read_grant_admits(self.read_grant, task_id, payer)


async def task_read_proof(
    x_task_token: str | None = Header(default=None, alias="X-Task-Token"),
    token: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_dispute_read_grant: str | None = Header(default=None, alias="X-Dispute-Read-Grant"),
) -> TaskReadProof:
    """FastAPI dependency resolving one request's task-read credentials.

    Accepts the token from the query as well as the header, for
    `require_task_read`'s reason — EventSource cannot set headers — so a route
    reachable from a stream is not a route with a second, weaker answer.
    """
    return TaskReadProof(
        task_token=x_task_token if x_task_token is not None else token,
        api_key=x_api_key,
        read_grant=x_dispute_read_grant,
    )


async def require_task_read(
    task_id: str,
    x_task_token: str | None = Header(default=None, alias="X-Task-Token"),
    token: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency guarding a `{task_id}`-scoped read route."""
    if not settings.task_auth_required:
        return
    # A valid operator API key sees everything (ops visibility). Through
    # `header_secret_matches`, so an operator whose key holds a non-ASCII
    # character is admitted here on exactly the terms `require_api_key` admits
    # them — two answers to "is this the operator?" would be one too many.
    if header_secret_matches(x_api_key, settings.api_key):
        return
    expected = state.task_tokens.get(task_id)
    supplied = x_task_token if x_task_token is not None else token
    if expected is None or supplied is None or not _matches(supplied, expected):
        # 404, not 403: a wrong token must be indistinguishable from a
        # nonexistent task, or ids become enumerable.
        #
        # A bare snake token, never the id. `main.http_exception_handler`
        # promotes a snake_case detail to `error.code` and derives the message
        # from it; an interpolated id is not a snake token, so it fell through
        # to the `not_found` fallback and the handler copied the CALLER'S OWN
        # TEXT into `error.message`. This dependency runs before the route's
        # own `max_length`, so that text was unbounded as well as reflected.
        # Nothing is lost: the id is in the path the caller sent, in the access
        # log line, and joinable to both through the request id.
        raise HTTPException(404, "unknown_task")
