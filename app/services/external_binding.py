"""Off-chain endpoint binding — story-1.06 ownership proof (Candidate A).

Binding an operator URL to an on-chain agent id must cost nothing but a
signature from the wallet that owns the agent — no password, email, or API key
(SOW Deliverable 1's whole premise). The flow:

  1. issue_challenge(agent_id) -> nonce. A random, short-lived value stored
     against the agent id.
  2. The operator signs the nonce's UTF-8 bytes with the secret key of the
     agent's on-chain owner (the G-address in the AgentRegistry `Agent`
     struct) — e.g. via StellarWalletsKit signMessage — and base64-encodes the
     signature.
  3. verify_challenge(agent_id, owner, signature_b64) checks that signature
     against `owner` and, on success, consumes the nonce (single use). The
     caller then records the (agent_id -> endpoint_url) binding.

This module is the spike prototype of steps 1 and 3 — pure and testable, with
an in-memory nonce store. The persistent binding table, the bind API endpoint,
and confirming `owner` against the live AgentRegistry are Epic 2.
"""

from __future__ import annotations

import base64
import secrets
import time

from stellar_sdk import Keypair

CHALLENGE_TTL_SECONDS = 300

# Domain separator and format version for the signed message. Domain-separated
# so an Orizon binding signature can never also be a valid signature for another
# protocol, and versioned so the format can move without a v1 signature silently
# still being accepted — a v1 signature simply stops verifying under a v2
# message.
BINDING_MESSAGE_PREFIX = "orizon-bind:v1"

# (agent_id, endpoint_url) -> (nonce_hex, expires_at). Keyed by the PAIR, not by
# the agent id alone: a challenge authorizes exactly one endpoint, and keying it
# by agent id let any anonymous caller destroy the honest owner's outstanding
# nonce simply by requesting a challenge for a URL they control.
_challenges: dict[tuple[str, str], tuple[str, float]] = {}


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
    """
    nonce = secrets.token_hex(16)
    expires_at = time.time() + ttl_seconds
    _challenges[(agent_id, endpoint_url)] = (nonce, expires_at)
    return nonce, expires_at


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
    """
    key = (agent_id, endpoint_url)
    entry = _challenges.get(key)
    if entry is None:
        return False
    nonce, expires_at = entry
    if time.time() > expires_at:
        del _challenges[key]
        return False
    message = binding_message(agent_id, endpoint_url, nonce)
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        Keypair.from_public_key(owner).verify(message.encode("utf-8"), signature)
    except ValueError:
        return False
    del _challenges[key]  # single use — a proven nonce never verifies twice
    return True
