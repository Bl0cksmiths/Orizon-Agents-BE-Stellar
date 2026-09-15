"""Off-chain endpoint binding — challenge, authority, and proof (ADR 0003).

Binding an operator URL to an on-chain agent id must cost nothing but a
signature from the wallet that owns the agent — no password, email, or API key
(SOW Deliverable 1's whole premise). The flow:

  1. issue_challenge(agent_id, endpoint_url) -> (nonce, expires_at). A random,
     short-lived value stored against the PAIR, and returned as-is while it is
     still live so a re-issue cannot cancel a bind already in progress.
  2. The operator signs binding_message(agent_id, endpoint_url, nonce) with the
     secret key of the agent's on-chain owner — e.g. via StellarWalletsKit
     signMessage — and base64-encodes the signature.
  3. resolve_owner(agent_id) reads that owner from the LIVE AgentRegistry, and
     verify_challenge(agent_id, endpoint_url, owner, signature_b64) checks the
     signature against it, consuming the nonce only on success. The caller then
     records the (agent_id -> endpoint_url) binding.

What 1.06 left to "Epic 2" is now here: the owner is confirmed against the live
registry (`resolve_owner`), and the bind API endpoint calls into this module.
Three things about it are load-bearing rather than incidental:

  - The SIGNED MESSAGE COVERS THE ENDPOINT, not just the nonce. The prototype
    signed the nonce alone, so a signature captured inside its five-minute
    window could be replayed to bind a different url (D3).
  - The OWNER READ FAILS CLOSED, inverting this repo's usual convention,
    because here the read is the authorization and nothing downstream re-checks
    it (D2). See `resolve_owner` for the full reasoning.
  - The NONCE TABLE IS BOUNDED. Both bind routes are public and unauthenticated
    by design, and the key is caller-supplied, so an unbounded table is memory
    exhaustion on a free instance.

The table is still process-local, so a bind in progress does not survive a
restart (the BINDING itself does — that is `binding_store`'s job). A caller who
was mid-signature simply asks for a new challenge.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from collections import OrderedDict

from stellar_sdk import Keypair

from ..config import settings
from ..stellar import cache as rcache
from ..stellar import client as sc

logger = logging.getLogger(__name__)

CHALLENGE_TTL_SECONDS = 300

# Domain separator and format version for the signed message. Domain-separated
# so an Orizon binding signature can never also be a valid signature for another
# protocol, and versioned so the format can move without a v1 signature silently
# still being accepted — a v1 signature simply stops verifying under a v2
# message.
BINDING_MESSAGE_PREFIX = "orizon-bind:v1"

# Retention cap for outstanding challenges (insertion-ordered eviction, see
# issue_challenge). Matches ramp_store._MAX_RAMPS, and for the same reason with
# sharper teeth: the challenge route is a PUBLIC, unauthenticated POST whose key
# is entirely caller-supplied, and the prototype's plain dict was swept only
# when a verify happened to hit that exact key — i.e. never, for keys an
# attacker never verifies. That is unbounded growth on a 512 MB free instance.
MAX_CHALLENGES = 500

# (agent_id, endpoint_url) -> (nonce_hex, expires_at). Keyed by the PAIR, not by
# the agent id alone: a challenge authorizes exactly one endpoint, and keying it
# by agent id let any anonymous caller destroy the honest owner's outstanding
# nonce simply by requesting a challenge for a URL they control.
_challenges: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()

# True while evictions are displacing LIVE challenges — flips the eviction log
# from WARNING (first) to DEBUG (consecutive), the registry_sync._failing
# discipline: a flood that keeps the table full must be visible once without
# the warning itself becoming the flood.
_evicting_live = False


def binding_message(agent_id: str, endpoint_url: str, nonce: str) -> str:
    """The exact UTF-8 string the agent's owner signs to prove a binding:

        orizon-bind:v1:{agent_id}:{endpoint_url}:{nonce}

    The endpoint is IN the signed bytes (ADR 0003 D3). The 1.06 prototype signed
    the nonce alone, so a signature captured inside its five-minute window could
    be replayed to bind a DIFFERENT url: the nonce proved "the owner signed
    something recently", never "the owner approved THIS endpoint".
    """
    return f"{BINDING_MESSAGE_PREFIX}:{agent_id}:{endpoint_url}:{nonce}"


def issue_challenge(agent_id: str, endpoint_url: str, ttl_seconds: int = CHALLENGE_TTL_SECONDS) -> tuple[str, float]:
    """Mint and store a challenge for (agent_id, endpoint_url).

    Returns (nonce, expires_at); the caller needs the expiry to tell the
    operator how long the challenge has left to be signed.

    Near-idempotent inside the window: a live, unexpired challenge for the same
    pair is returned AS IS, with whatever TTL it has left, rather than replaced.
    The route is public, so without this any anonymous caller could loop it and
    permanently grief the honest owner — every request minted a fresh nonce and
    invalidated the one the owner was in the middle of signing, so the owner's
    signature always arrived against a nonce that no longer existed. Re-issuing
    also cannot be used to extend a challenge's life past its original expiry.

    Bounded, sweep-on-insert — ramp_store.save's discipline: a new key that
    would take the table past MAX_CHALLENGES first evicts the oldest EXPIRED
    challenge, falling back to the oldest overall, so the table cannot exceed
    its cap however many agent ids an anonymous caller invents.
    """
    key = (agent_id, endpoint_url)
    live = _challenges.get(key)
    if live is not None and live[1] > time.time():
        return live
    if key not in _challenges and len(_challenges) >= MAX_CHALLENGES:
        _evict_one()
    nonce = secrets.token_hex(16)
    expires_at = time.time() + ttl_seconds
    _challenges[key] = (nonce, expires_at)
    _challenges.move_to_end(key)  # a replacement is a fresh insert for eviction order
    return nonce, expires_at


def _evict_one() -> None:
    """Drop one challenge to make room: the oldest EXPIRED one, or — if every
    outstanding challenge is still live — the oldest overall, so the table can
    never exceed its cap.

    Evicting a live challenge strands an operator who may be mid-signature, so
    it is worth saying out loud; the nonce is never logged (it is a live
    single-use credential) and consecutive evictions coalesce to DEBUG.
    """
    global _evicting_live
    now = time.time()
    victim = next(
        (k for k, (_, expires_at) in _challenges.items() if expires_at <= now),
        next(iter(_challenges)),
    )
    evicted = _challenges.pop(victim, None)
    if evicted is not None and evicted[1] > now:
        if _evicting_live:
            logger.debug("evicted a live bind challenge: agent_id=%s", victim[0])
        else:
            _evicting_live = True
            logger.warning(
                "bind challenge table full at %d — evicting LIVE challenges (agent_id=%s); a pending "
                "bind may have to be restarted (coalescing to DEBUG until it clears)",
                MAX_CHALLENGES,
                victim[0],
            )
    else:
        _evicting_live = False


# The owner lookup gets its OWN cache namespace, `agentowner:{agent_id}`. It must
# NEVER share `agent:{agent_id}` with read_agent/_agent_exists: that key is
# fail-OPEN and publicly pollable through GET /api/stellar/agent/{id}, so sharing
# it would let an attacker pre-warm the very cache this authorization decision
# reads from. 3 s, not the 15 s the marketplace reads use, because within the TTL
# a FORMER owner could still bind: the deployed AgentRegistry has no
# ownership-transfer entrypoint so that is unreachable today, and the short TTL
# is what stops the code from depending on that staying true (ADR 0003 D2).
OWNER_CACHE_TTL_SECONDS = 3.0

# Marks the RuntimeError sc.simulate_read raises when the CHAIN ANSWERED and the
# host function failed — `owner_of` panics on an id the registry does not hold.
# Every other exception (transport error, timeout, unloadable source account, no
# source address configured) means no answer came back at all, which is a very
# different thing and must not be read as "no such agent".
_SIMULATE_ERROR_PREFIX = "simulate failed:"


class OwnerLookupError(RuntimeError):
    """The chain could not be read — NOT "this agent has no owner".

    The router turns this into 503 `registry_unavailable`, never 404 and never
    "unknown owner, allow": "chain unreachable" and "no such agent" are
    different facts and the API has to say which one happened.
    """


def _describe(e: BaseException) -> str:
    """Compact "Type: message" description (registry_sync's helper) — bare type
    when there is no message, as asyncio.TimeoutError carries none."""
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


async def resolve_owner(agent_id: str) -> str | None:
    """The agent's owner G-address, read from the LIVE AgentRegistry. Returns
    None only when the agent genuinely is not on chain; raises OwnerLookupError
    when the chain could not be read.

    One single-agent `AgentRegistry.owner_of` read — never a `list_ids` scan.

    FAIL CLOSED, and note that this deliberately INVERTS this repo's convention.
    Every other registry read here fails OPEN: `_agent_exists`
    (app/routers/stellar.py:387-405) returns False on any exception, and
    `agent_id_available` says outright that "the chain's AlreadyExists is the
    real guard". That is correct THERE, because in each of those cases the chain
    still guards the operation downstream, so a wrong answer only buys a
    friendlier error message before a transaction fails. A binding has no
    downstream chain guard at all: nothing is submitted, nothing is signed by
    us, no contract re-checks anything — THIS READ IS THE AUTHORIZATION. Failing
    open here would read "RPC degrades, therefore anyone may bind any agent to
    any endpoint they control".

    `state.agents[agent_id].owner` is deliberately NOT used, even though it is
    already in memory and free. It is exactly the cached owner list AC-1 rules
    out: up to 15 s stale by design, stale INDEFINITELY through an RPC outage
    (the sync loop fails open and never dies), and None for seeded agents.
    Reading it would be an authorization bug, not an optimization.
    """
    # Read the setting LIVE rather than through the lru_cached sc.contract_ids():
    # that helper pins whatever id (or blank) it saw first for the life of the
    # process, the reason registry_sync.py:18-22 already records. The hermetic
    # suite forces this to "" (tests/conftest.py:44) — and with no registry
    # there is nothing to authorize against, so an unset id is a closed failure
    # rather than a free pass.
    contract_id = settings.stellar_agent_registry
    if not contract_id:
        raise OwnerLookupError("AgentRegistry is not configured (STELLAR_AGENT_REGISTRY is unset)")

    async def _fetch() -> str | None:
        try:
            owner = await asyncio.to_thread(sc.simulate_read, contract_id, "owner_of", [sc.sym(agent_id)])
        except Exception as e:
            if isinstance(e, RuntimeError) and str(e).startswith(_SIMULATE_ERROR_PREFIX):
                return None  # the chain answered and owner_of failed: no such agent
            raise OwnerLookupError(f"AgentRegistry.owner_of failed: {_describe(e)}") from e
        if isinstance(owner, str) and owner:
            return owner
        # The read succeeded but produced no address. That is not an answer we
        # can authorize against, so refuse rather than guess.
        raise OwnerLookupError(f"AgentRegistry.owner_of returned no address (got {type(owner).__name__})")

    try:
        result = await rcache.get_or_set(f"agentowner:{agent_id}", OWNER_CACHE_TTL_SECONDS, _fetch)
    except OwnerLookupError:
        raise
    except Exception as e:
        # Whatever else the cache layer surfaces — including a negative-cache
        # hit whose exception class could not be rebuilt — still means the
        # owner is unknown, and unknown is a refusal.
        raise OwnerLookupError(f"owner lookup failed: {_describe(e)}") from e
    return result if isinstance(result, str) else None


def _signature_matches(owner: str, message: str, signature_b64: str) -> bool:
    """True if `signature_b64` is `owner`'s ed25519 signature over `message` in
    EITHER of the two encodings real wallets produce.

    Wallets do not agree on what "sign a message" means. Some sign the raw UTF-8
    bytes; Freighter — which is what StellarWalletsKit's `signMessage` delegates
    to in the console — implements SEP-53 and signs
    `sha256(b"Stellar Signed Message:\\n" + message)` instead. Verifying only the
    raw form would make every real bind fail, and fail looking like "wrong
    owner" rather than the encoding mismatch it actually is.

    Accepting both is an INTEROPERABILITY widening, not a trust widening. The
    two payloads are derived deterministically from the SAME domain-separated
    message, which already pins the protocol, version, agent id, endpoint URL
    and nonce — so there is no message an attacker can get signed under one
    encoding that becomes a DIFFERENT authorization under the other. It must not
    be generalized any further: exactly these two, never a loop over candidate
    prefixes.

    `Keypair.verify_message` is the SDK's own SEP-53 implementation. Calling it
    rather than re-deriving the prefixed hash here keeps this in step with the
    spec instead of forking a second copy of the framing that can drift from it.
    """
    try:
        keypair = Keypair.from_public_key(owner)
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        # Malformed owner address or non-base64 signature — nothing to verify.
        return False
    try:
        keypair.verify(message.encode("utf-8"), signature)  # raw UTF-8 bytes
        return True
    except ValueError:
        pass
    try:
        keypair.verify_message(message, signature)  # SEP-53 prefixed + hashed
        return True
    except ValueError:
        return False


def verify_challenge(agent_id: str, endpoint_url: str, owner: str, signature_b64: str) -> bool:
    """Verify a base64 ed25519 signature over `binding_message(...)` against
    `owner` (a Stellar G-address). Consumes the nonce on success.

    Pure crypto and nonce lifecycle — NO chain I/O. `owner` is supplied by the
    caller, which must have resolved it from the live registry; keeping the two
    halves apart is what lets the hermetic suite test each without the other.

    Returns False on any failure — no or expired nonce, malformed owner or
    signature, or a signature that does not verify. Every failure mode
    (BadSignatureError, Ed25519PublicKeyInvalidError, binascii.Error) is a
    ValueError subclass, so one handler covers them without masking real bugs.
    A failed verification leaves the nonce ALONE: only a proven signature
    consumes it, so a bad guess cannot cancel the real owner's pending bind.
    """
    key = (agent_id, endpoint_url)
    entry = _challenges.get(key)
    if entry is None:
        return False
    nonce, expires_at = entry
    if time.time() > expires_at:
        del _challenges[key]
        return False
    if not _signature_matches(owner, binding_message(agent_id, endpoint_url, nonce), signature_b64):
        return False
    del _challenges[key]  # single use — a proven nonce never verifies twice
    return True
