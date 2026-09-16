"""Operator endpoint binding — story 2.01 (see docs/decisions/0003).

Binds an operator-hosted HTTPS endpoint to an on-chain agent id, authorised by
a signature from the wallet the AgentRegistry names as that agent's owner.

The wallet signature IS the credential — no API key, no password, no account
(SOW Deliverable 1's premise). That is why the write routes are public:
`require_api_key` guards the routes where the *backend* spends its own key, and
a shared secret cannot express "this caller owns *this* agent" anyway. Gating
them would also be a no-op on the demo, where API_KEY is unset.

The order of checks in `bind` is load-bearing, not stylistic; see its docstring.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from ..config import settings
from ..schemas import AGENT_ID_PATTERN
from ..services import external_binding
from ..services.binding_registry import note_bound
from ..services.binding_store import get_binding_store
from ..services.endpoint_policy import EndpointPolicyError, resolve_and_check, validate_endpoint_url
from ..services.external_binding import OwnerLookupError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["binding"])

# An ed25519 signature is exactly 64 bytes; base64 of that is 88 characters.
# Bounded at the edge so a megabyte of "signature" is rejected before decode.
_SIGNATURE_BYTES = 64


class BindChallengeReq(BaseModel):
    # The endpoint is part of the signed message (ADR 0003 D3), so it must be
    # supplied when the challenge is minted — not first seen at bind time.
    endpoint_url: str = Field(..., min_length=8, max_length=2048)


class BindChallengeResponse(BaseModel):
    agent_id: str
    nonce: str
    # The exact string the wallet must sign. Returned rather than assembled
    # client-side so the format can version without shipping a new frontend.
    message: str
    expires_at: float
    ttl_seconds: int


class BindReq(BaseModel):
    endpoint_url: str = Field(..., min_length=8, max_length=2048)
    # Upper bound only. A lower bound here would be caught by Pydantic and
    # answered as the generic `validation_error`, which is exactly the code the
    # frontend cannot map to an inline field error — so the *shape* of the
    # signature is settled by the handler instead, which can answer the stable
    # `signature_malformed`. max_length still bounds the decode: 256 chars is
    # ~4x an ed25519 signature's 88, so nothing useful is truncated.
    signature: str = Field(..., min_length=1, max_length=256, description="base64 ed25519 signature")


class BindingResponse(BaseModel):
    agent_id: str
    endpoint_url: str
    owner: str
    bound_at: float
    replaced: bool


class UnbindChallengeResponse(BaseModel):
    """Same shape as BindChallengeResponse, declared separately so the schema
    the console generates names the flow it belongs to — the two messages are
    NOT interchangeable and a shared model would suggest they were."""

    agent_id: str
    nonce: str
    # `orizon-unbind:v1:{agent_id}:{nonce}` — no endpoint; see
    # external_binding.unbinding_message for why.
    message: str
    expires_at: float
    ttl_seconds: int


class EndpointCheckResponse(BaseModel):
    allowed: bool
    rule: str | None = None
    message: str | None = None


def _host(url: str) -> str:
    """Host for a log line. Refusals log the host, never the full URL: the URL
    is attacker-controlled text landing in an operator's log viewer."""
    try:
        return urlsplit(url).hostname or "-"
    except ValueError:
        return "-"


def _is_operator(x_api_key: str | None) -> bool:
    """True for a caller holding the configured operator key.

    Deliberately NOT `require_api_key`: that dependency is a *no-op* while
    API_KEY is unset, which is the public-demo default — using it here would
    make every anonymous caller an operator and disclose every bound URL. With
    no key configured, nobody is privileged and the full URL is simply never
    disclosed.
    """
    expected = settings.api_key
    if not expected or x_api_key is None:
        return False
    return secrets.compare_digest(x_api_key.encode("utf-8", "ignore"), expected.encode("utf-8"))


async def _require_owner(agent_id: str) -> str:
    """The agent's on-chain owner, or the right HTTP error.

    Fail **closed**, inverting this repo's usual convention: `_agent_exists`
    and friends return a soft False on any exception because the chain still
    guards what follows. Nothing guards this — the owner read *is* the
    authorization — so an unreadable chain must be a retryable 503, never a
    404 ("we know it does not exist") and certainly never "owner unknown,
    proceed".
    """
    try:
        owner = await external_binding.resolve_owner(agent_id)
    except OwnerLookupError:
        logger.warning("bind refused: agent_id=%s reason=registry_unavailable", agent_id)
        raise HTTPException(503, "registry_unavailable") from None
    if owner is None:
        raise HTTPException(404, "agent_not_found")
    return owner


def _check_policy(agent_id: str, endpoint_url: str, stage: str) -> None:
    """Apply the pure SSRF rules, or 422. Logs the rule, which is what makes
    AC-4's "naming the rule" assertable server-side as well as in the API."""
    try:
        validate_endpoint_url(endpoint_url)
    except EndpointPolicyError as e:
        logger.warning(
            "%s refused: agent_id=%s reason=endpoint_not_allowed rule=%s host=%s",
            stage,
            agent_id,
            e.rule,
            _host(endpoint_url),
        )
        raise HTTPException(422, "endpoint_not_allowed") from None


# Declared before any "/{agent_id}/..." route, and as a three-segment path, so
# it can never be captured as an agent id — `GET /api/agents/{agent_id}` in
# app/routers/agents.py has no pattern constraint and would otherwise swallow
# a two-segment spelling regardless of router registration order.
@router.get("/bind/endpoint-check", response_model=EndpointCheckResponse, summary="Preflight an endpoint URL")
async def endpoint_check(url: Annotated[str, Query(min_length=1, max_length=2048)]) -> EndpointCheckResponse:
    """Advisory: would this URL be accepted? Pure — no DNS, no outbound request.

    Exists so the form can show an inline field error with the reason instead
    of a bare 422, the same trade `agent_id_available` makes for agent ids.
    """
    try:
        validate_endpoint_url(url)
    except EndpointPolicyError as e:
        return EndpointCheckResponse(allowed=False, rule=e.rule, message=str(e))
    return EndpointCheckResponse(allowed=True)


@router.post(
    "/{agent_id}/unbind/challenge",
    response_model=UnbindChallengeResponse,
    summary="Mint an unbind challenge to sign",
)
async def unbind_challenge(
    agent_id: str = Path(..., pattern=AGENT_ID_PATTERN),
) -> UnbindChallengeResponse:
    """Issue the nonce and the exact message the agent's owner must sign to
    revoke the binding.

    No request body: an unbind names no endpoint (see
    `external_binding.unbinding_message`), so there is nothing to supply.

    The chain read comes first for `bind_challenge`'s reason — the bounded
    challenge table may only ever hold real agent ids — and it has a second
    consequence here worth stating: an agent the registry cannot name an owner
    for cannot be unbound, because there is nobody to prove the revocation
    against. The deployed AgentRegistry has no way to remove an agent, so a
    binding cannot be orphaned this way today; if one ever gains it, the removal
    entrypoint has to revoke the binding as part of the same change rather than
    leave an endpoint nobody can detach.
    """
    await _require_owner(agent_id)

    nonce, expires_at = external_binding.issue_unbind_challenge(agent_id)
    # The nonce is a live single-use credential for its whole window: returned
    # to the caller, never written to a log.
    logger.info("unbind challenge issued: agent_id=%s", agent_id)
    return UnbindChallengeResponse(
        agent_id=agent_id,
        nonce=nonce,
        message=external_binding.unbinding_message(agent_id, nonce),
        expires_at=expires_at,
        ttl_seconds=external_binding.CHALLENGE_TTL_SECONDS,
    )


@router.post(
    "/{agent_id}/bind/challenge",
    response_model=BindChallengeResponse,
    summary="Mint a binding challenge to sign",
)
async def bind_challenge(
    body: BindChallengeReq,
    agent_id: str = Path(..., pattern=AGENT_ID_PATTERN),
) -> BindChallengeResponse:
    """Issue the nonce and the exact message the agent's owner must sign."""
    _check_policy(agent_id, body.endpoint_url, "challenge")
    # Refuse to mint for an id that is not on-chain, so the bounded challenge
    # table can only ever hold real agent ids.
    await _require_owner(agent_id)

    nonce, expires_at = external_binding.issue_challenge(agent_id, body.endpoint_url)
    # The nonce is a live single-use credential for its whole window: it is
    # returned to the caller and never written to a log.
    logger.info("binding challenge issued: agent_id=%s host=%s", agent_id, _host(body.endpoint_url))
    return BindChallengeResponse(
        agent_id=agent_id,
        nonce=nonce,
        message=external_binding.binding_message(agent_id, body.endpoint_url, nonce),
        expires_at=expires_at,
        ttl_seconds=external_binding.CHALLENGE_TTL_SECONDS,
    )


@router.post("/{agent_id}/bind", response_model=BindingResponse, summary="Bind an endpoint to an agent id")
async def bind(
    body: BindReq,
    agent_id: str = Path(..., pattern=AGENT_ID_PATTERN),
) -> BindingResponse:
    """Verify ownership and record the binding.

    The order below IS acceptance criteria 2 and 4, not house style:

    1. **Endpoint policy first** — before any chain read, nonce lookup or
       write, so a blocked URL provably never reaches the network and provably
       stores nothing.
    2. **Signature shape** — a pure decode. Cheap, and it keeps a client bug
       from costing an RPC round-trip.
    3. **Owner from the chain**, fail closed (see `_require_owner`).
    4. **Signature** — the nonce is consumed here and *only* on success, so an
       RPC outage can never burn a pending operator's challenge. Note this is
       also why owner resolution comes first: the nonce check happening before
       any chain read would make this a free unauthenticated `owner_of`
       amplifier, and consuming the nonce before the owner is known would let
       an outage grief every pending bind.
    5. **Resolve-and-check DNS** — after authorization, so an anonymous caller
       cannot use this as a free resolver.
    6. **Store.**
    """
    _check_policy(agent_id, body.endpoint_url, "bind")

    try:
        raw_signature = base64.b64decode(body.signature, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "signature_malformed") from None
    if len(raw_signature) != _SIGNATURE_BYTES:
        raise HTTPException(422, "signature_malformed")

    owner = await _require_owner(agent_id)

    if not external_binding.verify_challenge(agent_id, body.endpoint_url, owner, body.signature):
        # Deliberately ONE code for "no live challenge", "already used",
        # "expired" and "signature does not verify". Splitting them tells an
        # attacker which half of a captured signature is still live — the same
        # reasoning as require_task_read in app/services/task_auth.py. The
        # claimed signer is not logged: it is attacker-chosen noise, and the
        # request id already ties this line to the access log.
        logger.warning("bind refused: agent_id=%s reason=not_agent_owner", agent_id)
        raise HTTPException(401, "not_agent_owner")

    try:
        await resolve_and_check(body.endpoint_url)
    except EndpointPolicyError as e:
        logger.warning(
            "bind refused: agent_id=%s reason=endpoint_not_allowed rule=%s host=%s",
            agent_id,
            e.rule,
            _host(body.endpoint_url),
        )
        raise HTTPException(422, "endpoint_not_allowed") from None

    record = await get_binding_store().put(agent_id, body.endpoint_url, owner)
    # Make the agent routable to the planner immediately. The dispatch path
    # reads the store directly, but the planner's filter is synchronous and
    # works off an in-memory set, which would otherwise not know about this
    # agent until the next process start.
    note_bound(agent_id)
    replaced = record.previous_endpoint_url is not None
    # The accept path is the one place the full URL is logged — it is now
    # operator-declared configuration, not attacker-controlled text. `replaced`
    # plus the previous host is the AC-6 audit trail.
    logger.info(
        "endpoint bound: agent_id=%s owner=%s url=%s replaced=%s previous_host=%s",
        agent_id,
        owner,
        record.endpoint_url,
        replaced,
        _host(record.previous_endpoint_url) if record.previous_endpoint_url else "-",
    )
    return BindingResponse(
        agent_id=record.agent_id,
        endpoint_url=record.endpoint_url,
        owner=record.owner,
        bound_at=record.bound_at,
        replaced=replaced,
    )


@router.get("/{agent_id}/binding", response_model=BindingResponse, summary="Read an agent's binding")
async def read_binding(
    agent_id: str = Path(..., pattern=AGENT_ID_PATTERN),
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> BindingResponse:
    """The current binding. AC-6's "replaces the old endpoint" is only
    observable through this route.

    Anonymous callers get the **host only**: the binding is arguably public
    infrastructure, but publishing the full path invites traffic that bypasses
    the orchestrator entirely. A caller holding the operator key gets the
    whole URL.
    """
    record = await get_binding_store().get(agent_id)
    if record is None:
        raise HTTPException(404, "binding_not_found")

    disclosed = record.endpoint_url if _is_operator(x_api_key) else f"https://{_host(record.endpoint_url)}"
    return BindingResponse(
        agent_id=record.agent_id,
        endpoint_url=disclosed,
        owner=record.owner,
        bound_at=record.bound_at,
        replaced=record.previous_endpoint_url is not None,
    )
