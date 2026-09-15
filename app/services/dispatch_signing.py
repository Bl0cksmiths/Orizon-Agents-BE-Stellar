"""Dispatch envelope signing — proving an outbound POST is ours (ADR 0004 D2).

An operator receiving `POST {their endpoint}` from this service has, today, no
way to tell it came from Orizon: the envelope carries an agent id and a
dispatch id, both of which anyone can type. This module is the missing proof —
a SEP-53 signature over a message that names the destination, sent alongside
the body in three response-independent headers.

    X-Orizon-Signature          base64 ed25519 over dispatch_message(...)
    X-Orizon-Signature-Version  orizon-dispatch:v1
    X-Orizon-Signer             G… address that signed it

Four things about it are load-bearing rather than incidental:

  - THE MESSAGE BINDS THE DESTINATION. The endpoint URL is in the signed
    message and not in the body, so the verifier has to supply their OWN
    endpoint URL to check the signature. See `dispatch_message` for why that
    ordering is the entire point.
  - SEP-53, NEVER RAW BYTES. `Keypair.sign()` has no domain separation, so the
    only thing separating a dispatch signature from a transaction signature
    would be payload length. `sign_message` is the framing `external_binding`
    already verifies in the inbound direction.
  - A DEDICATED KEY, and an unset one is not an error. ORIZON_DISPATCH_SIGNING_KEY
    is deliberately not STELLAR_SIGNING_KEY (see app/config.py for the three
    operational reasons). Unset — the demo, read-only and CI default — means
    unsigned dispatch with one coalesced warning, never an exception: an
    unsigned dispatch is a gap the OPERATOR can close by rejecting it, and
    failing closed here would convert their policy into our outage.
  - NOTHING SECRET IS EVER LOGGED. Not the key, not the signature (ADR 0003's
    logging rule: the signature is exactly the "signature material"
    app/routers/stellar.py refuses to record), and not the rejected value of a
    malformed key — stellar_sdk's own exception text quotes the seed back, so
    that exception is swallowed rather than logged, exactly as
    `Settings._report_malformed_stellar_signing_key` swallows it.

Neither public helper raises. `dispatch_signer_address()` answers None and
`sign_dispatch()` answers {} when there is no usable key; the caller adds the
headers it is given and dispatches either way.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from stellar_sdk import Keypair

from ..config import settings

logger = logging.getLogger(__name__)

# Domain separator and format version for the signed message — `external_binding`'s
# BINDING_MESSAGE_PREFIX discipline pointed outward. Domain-separated so an Orizon
# dispatch signature can never also be a valid signature for another protocol (or
# for a binding proof), and versioned so the format can move without a v1 signature
# silently still being accepted: a v1 signature simply stops verifying under v2.
DISPATCH_SIG_VERSION = "orizon-dispatch:v1"

# Lowercase-hyphen on the wire either way (HTTP header names are case-insensitive
# and httpx sends these verbatim); spelled in the repo's header style, cf.
# `x-pdax-signature` in app/routers/pdax.py.
SIGNATURE_HEADER = "X-Orizon-Signature"
VERSION_HEADER = "X-Orizon-Signature-Version"
# CONVENIENCE ONLY. It says which key we believe we signed with, so an operator
# debugging a failed verification can see a mismatch instead of guessing — it is
# not, and must never be documented as, the key to verify against. Anyone can put
# any G… address in a header; an operator who trusts it is verifying a signature
# against a key the sender chose, which proves nothing. Operators MUST PIN the
# address out of band (our published key) and treat this header as a hint.
SIGNER_HEADER = "X-Orizon-Signer"


def dispatch_message(endpoint_url: str, body: bytes) -> str:
    """The exact string signed for one dispatch:

        orizon-dispatch:v1:{endpoint_url}:{sha256_hex(body)}

    THE URL IS IN THE MESSAGE, NOT THE BODY, and that is the single most
    important design point here. Because the endpoint is part of the signed
    bytes and is not transmitted, a verifier cannot check the signature without
    supplying their own endpoint URL — so cross-operator replay is structurally
    impossible instead of an optional field comparison an operator can forget.

    Put the URL in the body instead and operator A can take a fully-signed
    envelope we sent them and replay it at operator B: B verifies it against our
    published key, it passes, and B concludes Orizon dispatched work to them. The
    only thing standing between that and a forged job would be B remembering to
    compare a `url` field against themselves. This is ADR 0003 D3's lesson —
    the signature must cover the destination, not just the payload — pointed
    outward at dispatch.

    The body is hashed rather than embedded so the message stays a bounded,
    loggable-shaped string whatever the envelope grows into, and because the
    caller must sign the EXACT bytes it puts on the wire (ADR 0004: httpx's
    `json=` encoding differs from `json.dumps()` defaults, so serialize once and
    pass those bytes both here and to `content=`).
    """
    return f"{DISPATCH_SIG_VERSION}:{endpoint_url}:{hashlib.sha256(body).hexdigest()}"


# Derived keypair, memoized against the SECRET IT WAS DERIVED FROM rather than
# lru_cached on a no-argument call the way `client._signer_keypair` is. Deriving
# per dispatch would be wasteful on the outbound hot path, but a cache keyed on
# nothing pins whatever the process saw first — the exact failure
# `registry_sync` records for `sc.contract_ids()` — which would defeat runtime
# reconfiguration and leave the hermetic suite unable to set a key at all. A
# None keypair is a malformed value, cached so the failure is not re-derived
# either. No lock: the worst a race can do is derive the same key twice.
_cached: tuple[str, Keypair | None] = ("", None)

# Why dispatch is currently unsigned, or None while it is signed. Flips the
# report from WARNING (first) to DEBUG (while the reason is unchanged) and arms
# the INFO recovery line — the `registry_sync._failing` discipline, keyed by
# reason rather than a bare bool so a deployment that goes from unset to
# malformed still gets told once. An unsigned deployment must be visible ONCE:
# every dispatch takes this path, so an uncoalesced warning would be a log flood
# proportional to traffic, which is how a real signal gets filtered out.
_warned_reason: str | None = None


def _report_unsigned(reason: str, message: str) -> None:
    """Report that dispatch is going out unsigned — once per reason."""
    global _warned_reason
    if _warned_reason == reason:
        logger.debug("dispatch still unsigned (%s)", reason)
        return
    _warned_reason = reason
    logger.warning("%s (coalescing to DEBUG until it changes)", message)


def _report_signed() -> None:
    """Clear the degraded state and say so, once, when signing resumes.

    Called only from the path that actually produced a signature: "dispatch is
    signed again" is a claim only a completed signature can support, and
    resetting on a mere address lookup would let a persistent signing failure
    alternate INFO/WARNING forever instead of coalescing.
    """
    global _warned_reason
    if _warned_reason is not None:
        _warned_reason = None
        logger.info("dispatch signing enabled — outbound envelopes are signed")


def _derive(secret: str) -> Keypair | None:
    """Build the signing keypair, or None if the configured value is unusable.

    Accepts the two forms `client._signer_keypair` accepts — an S… secret or a
    12/24-word mnemonic — because an operator configuring this will copy the
    habits of STELLAR_SIGNING_KEY, and a mnemonic silently yielding unsigned
    dispatch is a worse outcome than four extra lines here.

    Returns None rather than raising, and the SDK exception is SWALLOWED whole:
    no message interpolation, no exc_info. stellar_sdk's exception text quotes
    the rejected seed back, so logging it would write a bearer credential into
    the log — the precedent, and the reasoning, is
    `Settings._report_malformed_stellar_signing_key`. The caller names the
    variable instead.
    """
    words = secret.split()
    try:
        if len(words) >= 12:
            return Keypair.from_mnemonic_phrase(" ".join(words))
        return Keypair.from_secret(secret)
    except Exception:
        return None


def _signer() -> Keypair | None:
    """The dispatch signing keypair, or None when dispatch must go unsigned.

    NEVER RAISES — which is the whole reason this exists instead of a call to
    `client._signer_keypair`, whose contract is the opposite (it raises on an
    empty key, and a raise on the dispatch path would turn "this deployment has
    no dispatch key" into a failed step for an operator who did nothing wrong).

    The setting is read LIVE on every call, never captured at import: the value
    is what the hermetic suite mutates, and a deployment that has just had the
    key injected should start signing without a restart.
    """
    global _cached
    secret = (settings.orizon_dispatch_signing_key or "").strip()
    if not secret:
        _report_unsigned(
            "unset",
            "ORIZON_DISPATCH_SIGNING_KEY is not set — outbound dispatch is UNSIGNED, so an "
            "operator cannot prove the request came from Orizon and may reject it. Set it to a "
            "dedicated S… secret (NOT the settler key in STELLAR_SIGNING_KEY)",
        )
        return None
    if _cached[0] != secret:
        _cached = (secret, _derive(secret))
    keypair = _cached[1]
    if keypair is None:
        _report_unsigned(
            "malformed",
            "ORIZON_DISPATCH_SIGNING_KEY is malformed — it is neither a valid S… secret key nor a "
            "valid 12/24-word mnemonic phrase (the value is withheld from this log). Outbound "
            "dispatch is UNSIGNED until it is re-injected from host secrets",
        )
    return keypair


def dispatch_signer_address() -> str | None:
    """The G… address operators should expect to have signed our dispatches, or
    None when this deployment has no usable key and therefore signs nothing.

    Never raises: it is read by a public network-config route as much as by the
    dispatch path, and neither should 500 over an absent optional key.

    Publishing this address is what makes the signature worth anything — an
    operator pins it out of band and verifies against the pinned value, never
    against X-Orizon-Signer.
    """
    keypair = _signer()
    return keypair.public_key if keypair is not None else None


def sign_dispatch(endpoint_url: str, body: bytes) -> dict[str, str]:
    """Signature headers for one dispatch to `endpoint_url` carrying exactly
    `body` — or {} when this deployment has no usable key.

    `body` must be the BYTES THAT GO ON THE WIRE, not a dict re-serialized here:
    signing one encoding and sending another produces a signature over bytes the
    operator never receives, and it breaks only once the payload contains
    non-ASCII (ADR 0004). The caller serializes once and passes those bytes both
    here and to the request.

    `Keypair.sign_message` is the SDK's own SEP-53 implementation:
    `sign(sha256(b"Stellar Signed Message:\n" + message))`. Calling it rather
    than re-deriving the prefixed hash keeps this in step with the spec instead
    of forking a second copy of the framing — `external_binding._signature_matches`
    makes the same choice for the inbound direction, which is what keeps the two
    halves of this protocol describing the same thing. `Keypair.sign()` is NEVER
    used: it signs raw bytes with no domain separation at all, so a signature
    over an attacker-shaped "endpoint" would be structurally indistinguishable
    from a signature over a transaction envelope, separated only by length.

    Returns {} — an empty mapping the caller can splat into its headers — rather
    than raising or returning None, so the dispatch path has no branch to forget
    and an unsigned deployment sends the same request minus three headers.

    Never logs the signature. It is the "signature material" ADR 0003 refuses to
    record, and an attacker who can read it out of a log can replay a dispatch
    we made to one operator for as long as that envelope stays plausible.
    """
    keypair = _signer()
    if keypair is None:
        return {}
    try:
        signature = keypair.sign_message(dispatch_message(endpoint_url, body))
    except Exception as e:
        # Unreachable by construction — _signer only ever returns a keypair
        # built from a secret, so the seed is present. Caught anyway because
        # "never raises" is this module's contract and it is called from the
        # outbound HTTP path: an unsigned dispatch is a degraded dispatch, while
        # an exception here would fail a step the operator would have accepted.
        # The type is named, never the message: it could quote key material.
        _report_unsigned(
            "sign_failed",
            f"dispatch signing failed with {type(e).__name__} — outbound dispatch is UNSIGNED",
        )
        return {}
    _report_signed()
    return {
        SIGNATURE_HEADER: base64.b64encode(signature).decode("ascii"),
        VERSION_HEADER: DISPATCH_SIG_VERSION,
        SIGNER_HEADER: keypair.public_key,
    }
