"""Endpoint-binding challenge lifecycle and crypto (story 1.06 spike, reworked
for 2.01 / ADR 0003 D3).

Binding an operator URL to an on-chain agent id must require nothing but a
signature from the wallet that owns the agent — no account, password, email, or
API key. These tests exercise the challenge/verify half directly with a real
Stellar keypair; the chain half (`resolve_owner`) lives in
test_external_binding_authority.py precisely because the two are separable.

The 2.01 additions are the replay fixes: the signature now covers the endpoint,
so a signature captured for one URL cannot bind another, and a signature in the
old bare-nonce format no longer verifies at all.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from stellar_sdk import Keypair

from app.services.external_binding import (
    BINDING_MESSAGE_PREFIX,
    binding_message,
    issue_challenge,
    verify_challenge,
)

ENDPOINT = "https://ops.example.com/agent"
OTHER_ENDPOINT = "https://attacker.example.net/agent"


def _sign(kp: Keypair, message: str) -> str:
    """What StellarWalletsKit signMessage produces: base64 of the ed25519
    signature over the message's UTF-8 bytes."""
    return base64.b64encode(kp.sign(message.encode("utf-8"))).decode("ascii")


def _sign_challenge(kp: Keypair, agent_id: str, endpoint_url: str, nonce: str) -> str:
    return _sign(kp, binding_message(agent_id, endpoint_url, nonce))


def test_binding_message_is_domain_separated_and_versioned() -> None:
    # The exact bytes the frontend will sign — pinned, because changing them
    # silently invalidates every signature the wallets produce.
    assert binding_message("ext_a", ENDPOINT, "cafe") == f"orizon-bind:v1:ext_a:{ENDPOINT}:cafe"
    assert BINDING_MESSAGE_PREFIX == "orizon-bind:v1"


def test_valid_wallet_signature_binds_and_is_single_use() -> None:
    owner = Keypair.random()
    nonce, expires_at = issue_challenge("ext_a", ENDPOINT)

    assert expires_at > 0
    sig = _sign_challenge(owner, "ext_a", ENDPOINT, nonce)
    assert verify_challenge("ext_a", ENDPOINT, owner.public_key, sig) is True
    # single use: the proven nonce is consumed, so replaying it cannot bind again
    assert verify_challenge("ext_a", ENDPOINT, owner.public_key, sig) is False


def test_tampered_signature_is_rejected() -> None:
    owner = Keypair.random()
    nonce, _ = issue_challenge("ext_b", ENDPOINT)
    # signs bytes that are not the issued challenge message
    sig = _sign(owner, binding_message("ext_b", ENDPOINT, nonce) + "tamper")
    assert verify_challenge("ext_b", ENDPOINT, owner.public_key, sig) is False


def test_signature_from_a_different_wallet_is_rejected() -> None:
    owner = Keypair.random()
    attacker = Keypair.random()
    nonce, _ = issue_challenge("ext_c", ENDPOINT)
    # a valid signature — but from a wallet that does not own the agent
    sig = _sign_challenge(attacker, "ext_c", ENDPOINT, nonce)
    assert verify_challenge("ext_c", ENDPOINT, owner.public_key, sig) is False


def test_no_outstanding_challenge_is_rejected() -> None:
    owner = Keypair.random()
    # nothing was ever issued for this agent id
    bogus = base64.b64encode(b"x" * 64).decode("ascii")
    assert verify_challenge("ext_never_issued", ENDPOINT, owner.public_key, bogus) is False


def test_expired_challenge_is_rejected() -> None:
    owner = Keypair.random()
    nonce, _ = issue_challenge("ext_d", ENDPOINT, ttl_seconds=-1)  # already expired at issue
    sig = _sign_challenge(owner, "ext_d", ENDPOINT, nonce)
    assert verify_challenge("ext_d", ENDPOINT, owner.public_key, sig) is False


def test_malformed_owner_address_is_rejected() -> None:
    owner = Keypair.random()
    nonce, _ = issue_challenge("ext_e", ENDPOINT)
    # signature is genuine; the owner address is not a valid G-address
    sig = _sign_challenge(owner, "ext_e", ENDPOINT, nonce)
    assert verify_challenge("ext_e", ENDPOINT, "not-a-valid-stellar-address", sig) is False


def test_malformed_signature_is_rejected() -> None:
    owner = Keypair.random()
    issue_challenge("ext_f", ENDPOINT)
    # not base64 at all, and base64 of the wrong length — both are ValueErrors
    assert verify_challenge("ext_f", ENDPOINT, owner.public_key, "not base64!!") is False
    assert verify_challenge("ext_f", ENDPOINT, owner.public_key, base64.b64encode(b"short").decode()) is False


def test_old_bare_nonce_signature_does_not_verify() -> None:
    """The 1.06 format — sign the nonce alone — must be worthless now.

    This is the whole point of ADR 0003 D3: a signature that says nothing about
    the endpoint cannot be allowed to authorize one. The nonce is still live and
    the wallet is still the owner's; only the signed bytes changed.
    """
    owner = Keypair.random()
    nonce, _ = issue_challenge("ext_g", ENDPOINT)
    old_format = _sign(owner, nonce)  # exactly what the prototype accepted
    assert verify_challenge("ext_g", ENDPOINT, owner.public_key, old_format) is False
    # and the nonce was NOT consumed by the failed attempt — the real owner can
    # still complete the bind with a correctly-formed signature
    assert verify_challenge("ext_g", ENDPOINT, owner.public_key, _sign_challenge(owner, "ext_g", ENDPOINT, nonce))


def test_signature_for_one_endpoint_cannot_bind_another() -> None:
    """The replay fix, stated directly.

    Both endpoints are given the SAME nonce, so the only thing distinguishing
    them is the signed message. Under the prototype (nonce signed alone) the
    captured signature verified for either URL, which is exactly how a captured
    signature bound an attacker's host.
    """
    owner = Keypair.random()
    nonce, _ = issue_challenge("ext_h", ENDPOINT)
    # plant the identical nonce against the attacker's URL so the test isolates
    # the message binding rather than the (agent_id, endpoint_url) keying
    from app.services import external_binding as eb

    _, expires_at = eb._challenges[("ext_h", ENDPOINT)]
    eb._challenges[("ext_h", OTHER_ENDPOINT)] = (nonce, expires_at)

    sig = _sign_challenge(owner, "ext_h", ENDPOINT, nonce)
    assert verify_challenge("ext_h", OTHER_ENDPOINT, owner.public_key, sig) is False
    # the same signature is still good for the endpoint it was actually made for
    assert verify_challenge("ext_h", ENDPOINT, owner.public_key, sig) is True


def test_reissue_inside_the_window_returns_the_same_challenge() -> None:
    """The anti-griefing property.

    The challenge route is public, so if every request minted a fresh nonce an
    anonymous caller could loop it and make sure the honest owner's signature
    always arrived against a nonce that had just been replaced.
    """
    nonce, expires_at = issue_challenge("ext_i", ENDPOINT)
    # same nonce AND the original expiry — re-issuing cannot extend a challenge
    assert issue_challenge("ext_i", ENDPOINT) == (nonce, expires_at)
    # a different endpoint for the same agent is a different challenge, and
    # asking for it must not disturb the one already outstanding
    other_nonce, _ = issue_challenge("ext_i", OTHER_ENDPOINT)
    assert other_nonce != nonce
    assert issue_challenge("ext_i", ENDPOINT) == (nonce, expires_at)


def test_an_expired_challenge_is_replaced_rather_than_returned() -> None:
    stale_nonce, stale_expiry = issue_challenge("ext_j", ENDPOINT, ttl_seconds=-1)
    fresh_nonce, fresh_expiry = issue_challenge("ext_j", ENDPOINT)
    assert fresh_nonce != stale_nonce
    assert fresh_expiry > stale_expiry


def test_challenge_table_is_bounded_at_the_bind_budget() -> None:
    """An unauthenticated POST whose key is caller-supplied must not be able to
    grow the table without limit — that is memory exhaustion on a free instance.

    Bounded by the BIND budget rather than the whole table since 4.07: the
    endpoint URL in a bind key is caller-supplied and unbounded, which made
    this the one flood an attacker could mount at will, and a shared bound
    meant mounting it displaced the unbind and dispute challenges too. Full,
    the budget REFUSES a new mint instead of taking a live one.
    """
    from app.services import external_binding as eb

    saved = eb._challenges.copy()
    eb._challenges.clear()
    eb._exhausted.clear()
    try:
        for i in range(eb.CHALLENGE_BUDGETS["bind"]):
            issue_challenge(f"flood_{i}", ENDPOINT)
        assert len(eb._challenges) == eb.CHALLENGE_BUDGETS["bind"]

        with pytest.raises(eb.ChallengeBudgetExhausted) as info:
            issue_challenge("flood_one_too_many", ENDPOINT)

        assert info.value.purpose == "bind"
        assert len(eb._challenges) == eb.CHALLENGE_BUDGETS["bind"]
        # The operator who got in first still holds a signable challenge.
        assert ("flood_0", ENDPOINT) in eb._challenges
    finally:
        eb._challenges.clear()
        eb._challenges.update(saved)
        eb._exhausted.clear()


def test_a_full_budget_sweeps_expired_challenges_and_never_live_ones() -> None:
    """The live challenge is the OLDEST entry, so a naive oldest-first eviction
    would take it and strand the one operator actually mid-bind."""
    from app.services import external_binding as eb

    saved = eb._challenges.copy()
    eb._challenges.clear()
    eb._exhausted.clear()
    try:
        issue_challenge("still_binding", ENDPOINT)
        for i in range(eb.CHALLENGE_BUDGETS["bind"] - 1):
            issue_challenge(f"stale_{i}", ENDPOINT, ttl_seconds=-1)
        assert len(eb._challenges) == eb.CHALLENGE_BUDGETS["bind"]

        issue_challenge("newcomer", ENDPOINT)

        # The lapsed entries were reclaimed, so the newcomer got in without
        # anything live being touched.
        assert ("still_binding", ENDPOINT) in eb._challenges
        assert ("newcomer", ENDPOINT) in eb._challenges
        assert ("stale_0", ENDPOINT) not in eb._challenges
    finally:
        eb._challenges.clear()
        eb._challenges.update(saved)
        eb._exhausted.clear()


# ── SEP-53: what a real wallet actually signs ────────────────────────────
SEP53_PREFIX = b"Stellar Signed Message:\n"


def _sign_sep53(kp: Keypair, message: str) -> str:
    """What Freighter produces through StellarWalletsKit's signMessage: base64
    of the ed25519 signature over sha256(prefix + message).

    Built from the SEP-53 spec by hand rather than from Keypair.sign_message, so
    these tests prove the wire format the browser will send instead of proving
    only that the SDK agrees with itself.
    """
    digest = hashlib.sha256(SEP53_PREFIX + message.encode("utf-8")).digest()
    return base64.b64encode(kp.sign(digest)).decode("ascii")


def test_hand_built_sep53_payload_matches_the_sdk_framing() -> None:
    # verify_challenge accepts the SEP-53 form via Keypair.verify_message. If
    # the SDK's framing ever moved, real wallet signatures would silently stop
    # verifying — this pins the spec construction and the SDK's to each other.
    kp = Keypair.random()
    message = binding_message("ext_k", ENDPOINT, "cafe")
    assert base64.b64decode(_sign_sep53(kp, message)) == kp.sign_message(message)


def test_sep53_signed_message_binds_and_is_single_use() -> None:
    owner = Keypair.random()
    nonce, _ = issue_challenge("ext_l", ENDPOINT)
    sig = _sign_sep53(owner, binding_message("ext_l", ENDPOINT, nonce))

    assert verify_challenge("ext_l", ENDPOINT, owner.public_key, sig) is True
    assert verify_challenge("ext_l", ENDPOINT, owner.public_key, sig) is False


def test_both_signing_encodings_of_the_same_message_are_accepted() -> None:
    """Wallets disagree on what "sign a message" means; a raw-bytes wallet and a
    SEP-53 wallet must both be able to complete a bind."""
    owner = Keypair.random()
    raw_nonce, _ = issue_challenge("ext_m", ENDPOINT)
    sep_nonce, _ = issue_challenge("ext_n", ENDPOINT)

    raw_sig = _sign(owner, binding_message("ext_m", ENDPOINT, raw_nonce))
    sep_sig = _sign_sep53(owner, binding_message("ext_n", ENDPOINT, sep_nonce))
    assert verify_challenge("ext_m", ENDPOINT, owner.public_key, raw_sig) is True
    assert verify_challenge("ext_n", ENDPOINT, owner.public_key, sep_sig) is True


def test_sep53_framing_does_not_widen_what_counts_as_authorized() -> None:
    """Two accepted encodings must not become two ways in.

    Neither the old bare-nonce format, nor a signature made for another
    endpoint, nor one from another wallet may pass just because it arrives
    SEP-53 framed — and none of those failures may burn the live nonce.
    """
    owner = Keypair.random()
    attacker = Keypair.random()
    nonce, _ = issue_challenge("ext_o", ENDPOINT)

    # the 1.06 bare-nonce format, SEP-53 framed
    assert verify_challenge("ext_o", ENDPOINT, owner.public_key, _sign_sep53(owner, nonce)) is False
    # the owner's own signature, but made for a different endpoint
    for_other = _sign_sep53(owner, binding_message("ext_o", OTHER_ENDPOINT, nonce))
    assert verify_challenge("ext_o", ENDPOINT, owner.public_key, for_other) is False
    # a correctly-formed signature from a wallet that does not own the agent
    from_attacker = _sign_sep53(attacker, binding_message("ext_o", ENDPOINT, nonce))
    assert verify_challenge("ext_o", ENDPOINT, owner.public_key, from_attacker) is False

    # none of that consumed the nonce — the real owner can still bind
    good = _sign_sep53(owner, binding_message("ext_o", ENDPOINT, nonce))
    assert verify_challenge("ext_o", ENDPOINT, owner.public_key, good) is True
