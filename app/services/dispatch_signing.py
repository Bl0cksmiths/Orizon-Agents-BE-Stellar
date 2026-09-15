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

import hashlib
import logging

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
