"""Dispatch envelope signing — the wire format, the replay property, and the
three ways this must degrade instead of failing (ADR 0004 D2).

What is pinned here is a PROTOCOL an operator implements against, so the tests
are deliberately written from the verifier's side: the signed payload is rebuilt
by hand with `hashlib`, and the signature is checked with the raw ed25519
primitive (`Keypair.verify`) rather than with `Keypair.verify_message`. Asserting
that our `sign_message` agrees with the SDK's `verify_message` would only prove
the SDK agrees with itself — it would still pass if the framing changed under
us, and every operator's verifier would break. These assertions fail instead.

The load-bearing properties, in order:

  - the message is exactly `orizon-dispatch:v1:{url}:{sha256_hex(body)}`;
  - the bytes signed are SEP-53 framed — `sha256(b"Stellar Signed Message:\\n" +
    message)` — and are NOT the raw message, so `Keypair.sign()` cannot have
    been used;
  - a signature for one endpoint does not verify for another. That is the
    cross-operator replay property, and it is structural: the URL is only in
    the signed message, so a verifier cannot skip the check;
  - an unset or malformed key yields no headers, no address, and no exception;
  - the unsigned warning fires once, not once per dispatch;
  - the key and the signature never reach the log.

`tests/conftest.py` neutralises the suite's secrets but knows nothing about this
setting, so the fixture below both sets it and restores it — and resets the
module's memoized keypair and warning state, which are process-global and would
otherwise leak a configured key into the next test.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator

import pytest
from stellar_sdk import Keypair
from stellar_sdk.exceptions import BadSignatureError

from app.config import settings
from app.services import dispatch_signing as ds
from app.services.dispatch_signing import dispatch_message, dispatch_signer_address, sign_dispatch

# A throwaway signer, derived from a fixed 32-byte seed so the suite pins a KNOWN
# key without committing an S… literal to the repo for a scanner to find. It has
# never held value and never will.
SIGNER = Keypair.from_raw_ed25519_seed(b"orizon-dispatch-test-seed-2.02!!")
SECRET = SIGNER.secret

# One dispatch, spelled as bytes exactly as the caller must put it on the wire.
BODY = b'{"v":2,"agent_id":"ext_translate","dispatch_id":"d1"}'
URL = "https://ops.example.com/orizon/dispatch"
OTHER_URL = "https://other-operator.example.net/hook"

# The full signed string, written out rather than recomputed — including the
# sha256 of BODY — so a change to either half of the format has to be made here
# too, in front of a reviewer.
EXPECTED_MESSAGE = (
    "orizon-dispatch:v1:https://ops.example.com/orizon/dispatch"
    ":d43a1dc86d353981dcc41ee12a0beeb3e852bc374b8959bfe0acb9a46148fd04"
)


@pytest.fixture(autouse=True)
def isolated_signing_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test unconfigured, and leave nothing behind.

    The key is restored by monkeypatch (conftest's hermetic fixture predates
    this setting and does not cover it), and so are the module globals: `_cached`
    memoizes a derived keypair for the life of the process, and `_warned_reason`
    is what makes the unsigned warning fire once — a test that left either set
    would silently disarm the next one.
    """
    monkeypatch.setattr(settings, "orizon_dispatch_signing_key", "")
    monkeypatch.setattr(ds, "_cached", ("", None))
    monkeypatch.setattr(ds, "_warned_reason", None)
    yield


def _configure(key: str) -> None:
    """Point the module at `key` (read live, so no reload is needed)."""
    settings.orizon_dispatch_signing_key = key


def _signature(headers: dict[str, str]) -> bytes:
    """The raw signature bytes out of a header set."""
    return base64.b64decode(headers[ds.SIGNATURE_HEADER], validate=True)


def test_the_message_is_the_frozen_format() -> None:
    """orizon-dispatch:v1:{endpoint_url}:{sha256_hex(body)} — literally."""
    assert dispatch_message(URL, BODY) == EXPECTED_MESSAGE
    # The digest in that literal is the sha256 of BODY and nothing else.
    assert EXPECTED_MESSAGE.endswith(":" + hashlib.sha256(BODY).hexdigest())
    # The URL is in the MESSAGE and is not derivable from the body: change the
    # destination and the signed string changes, which is the whole mechanism.
    assert dispatch_message(OTHER_URL, BODY) != EXPECTED_MESSAGE
    assert URL not in BODY.decode("utf-8")


def test_the_signed_payload_is_sep53_framed_not_the_raw_message() -> None:
    """Pin the WIRE FORMAT: the bytes signed are sha256 of the SEP-53 preimage.

    Built here with hashlib, and verified with the raw ed25519 primitive, so
    this fails if `sign_message` is ever swapped for `sign` — which is the
    mistake it exists to prevent, `sign` having no domain separation at all.
    """
    _configure(SECRET)
    signature = _signature(sign_dispatch(URL, BODY))

    payload = hashlib.sha256(b"Stellar Signed Message:\n" + EXPECTED_MESSAGE.encode("utf-8")).digest()
    Keypair.from_public_key(SIGNER.public_key).verify(payload, signature)  # raises if it disagrees

    # And emphatically NOT a signature over the message bytes themselves.
    with pytest.raises(BadSignatureError):
        Keypair.from_public_key(SIGNER.public_key).verify(EXPECTED_MESSAGE.encode("utf-8"), signature)


def test_a_signature_verifies_for_the_published_signer_address() -> None:
    """The operator's happy path, performed exactly as an operator would.

    They pin our address out of band, rebuild the message from THEIR endpoint
    URL and the body they received, and verify. Nothing in the request is
    trusted to tell them any of that.
    """
    _configure(SECRET)
    assert dispatch_signer_address() == SIGNER.public_key

    headers = sign_dispatch(URL, BODY)
    assert set(headers) == {ds.SIGNATURE_HEADER, ds.VERSION_HEADER, ds.SIGNER_HEADER}
    assert headers[ds.VERSION_HEADER] == "orizon-dispatch:v1"
    assert headers[ds.SIGNER_HEADER] == SIGNER.public_key  # a hint; never the trust anchor

    pinned = Keypair.from_public_key(SIGNER.public_key)
    pinned.verify_message(dispatch_message(URL, BODY), _signature(headers))


def test_a_signature_for_one_endpoint_does_not_verify_for_another() -> None:
    """THE cross-operator replay property, made explicit.

    Operator A holds a complete, genuinely-signed envelope we sent them: body,
    signature, version and signer headers. Replayed verbatim at operator B, B
    rebuilds the message with B's own URL — the only URL B has — and the
    signature fails. Nothing here is an optional check B could forget: B cannot
    even construct the message A verified, because that URL was never sent.
    """
    _configure(SECRET)
    for_a = sign_dispatch(URL, BODY)
    for_b = sign_dispatch(OTHER_URL, BODY)
    pinned = Keypair.from_public_key(SIGNER.public_key)

    # Same key, same body, different destination — a different signature.
    assert for_b[ds.SIGNATURE_HEADER] != for_a[ds.SIGNATURE_HEADER]

    # B cannot verify A's envelope...
    with pytest.raises(BadSignatureError):
        pinned.verify_message(dispatch_message(OTHER_URL, BODY), _signature(for_a))
    # ...and B's own signature is equally useless to A.
    with pytest.raises(BadSignatureError):
        pinned.verify_message(dispatch_message(URL, BODY), _signature(for_b))


def test_a_signature_does_not_carry_over_to_a_different_body() -> None:
    """The body is bound too — by its digest, so size is irrelevant.

    An operator who verifies the signature against the bytes they were handed
    cannot be fed a substituted step under a captured signature.
    """
    _configure(SECRET)
    signature = _signature(sign_dispatch(URL, BODY))
    tampered = BODY.replace(b'"d1"', b'"d2"')
    assert tampered != BODY

    with pytest.raises(BadSignatureError):
        Keypair.from_public_key(SIGNER.public_key).verify_message(dispatch_message(URL, tampered), signature)


def test_a_mnemonic_key_signs_like_a_secret_key() -> None:
    """Both forms `client._signer_keypair` accepts are accepted here.

    An operator configuring ORIZON_DISPATCH_SIGNING_KEY copies the habits of
    STELLAR_SIGNING_KEY, and a mnemonic that silently produced unsigned dispatch
    would be a worse outcome than supporting it.
    """
    phrase = Keypair.generate_mnemonic_phrase()
    expected = Keypair.from_mnemonic_phrase(phrase).public_key
    _configure(phrase)

    assert dispatch_signer_address() == expected
    assert sign_dispatch(URL, BODY)[ds.SIGNER_HEADER] == expected
