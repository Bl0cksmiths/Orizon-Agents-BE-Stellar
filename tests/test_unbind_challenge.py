"""The proof that authorises an UNBIND — the other half of ADR 0003's challenge.

tests/test_external_binding_spike.py pins the bind challenge. This file pins the
revocation one, and nearly all of it is about the single property the design
turns on: A BIND SIGNATURE MUST NOT BE USABLE AS AN UNBIND, and vice versa.

That matters because a bind signature is not a secret. It is handed to a public,
unauthenticated route, it may sit in a browser's network log or a proxy's
history, and it stays syntactically valid forever. If it could also revoke, then
anyone who ever observed an operator binding an endpoint could take that
operator's agent out of the network at will — and the operator would have no way
to tell that from their own unbind. Two distinct domain separators are what make
that impossible, so the tests here assert the separation from both directions
rather than trusting that the two code paths merely look different.

The rest covers what the unbind flow inherits by REUSING `issue_challenge`
instead of forking it — single use, expiry, idempotency inside the window, the
bounded table — because the value of that reuse is precisely that these
properties did not have to be re-implemented, and a future refactor that forks
them should fail here.

Hermetic: pure crypto and an in-process table, no chain and no network.
"""

from __future__ import annotations

import base64

import pytest
from stellar_sdk import Keypair

from app.services import external_binding as eb
from app.services.endpoint_policy import EndpointPolicyError, validate_endpoint_url
from app.services.external_binding import (
    UNBIND_SUBJECT,
    UNBINDING_MESSAGE_PREFIX,
    binding_message,
    issue_challenge,
    issue_unbind_challenge,
    unbinding_message,
    verify_challenge,
    verify_unbind_challenge,
)

ENDPOINT = "https://operator.example/run"


def _sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def _sign_sep53(keypair: Keypair, message: str) -> str:
    """What Freighter produces: sign(sha256(b"Stellar Signed Message:\\n" + msg))."""
    return base64.b64encode(keypair.sign_message(message.encode("utf-8"))).decode("ascii")


def _sign_unbind(keypair: Keypair, agent_id: str, nonce: str) -> str:
    return _sign(keypair, unbinding_message(agent_id, nonce))


# ── the message ─────────────────────────────────────────────────


def test_the_unbind_message_is_domain_separated_and_versioned() -> None:
    assert UNBINDING_MESSAGE_PREFIX == "orizon-unbind:v1"
    assert unbinding_message("ext_a", "cafe") == "orizon-unbind:v1:ext_a:cafe"


def test_the_unbind_message_carries_no_endpoint() -> None:
    # An unbind revokes whatever is bound, and the operator of a compromised
    # host may not be able to name the URL — `GET /{id}/binding` gives an
    # anonymous caller the host alone. Requiring it would make revocation
    # hardest in exactly the case it exists for.
    message = unbinding_message("ext_a", "cafe")
    assert "http" not in message
    assert message.count(":") == 3  # prefix, version, agent id, nonce


def test_the_two_messages_never_collide() -> None:
    # Not "they happen to differ" — the prefixes are different domains, so no
    # choice of agent id, endpoint or nonce can make one equal the other.
    assert not unbinding_message("ext_a", "cafe").startswith(eb.BINDING_MESSAGE_PREFIX)
    assert not binding_message("ext_a", ENDPOINT, "cafe").startswith(UNBINDING_MESSAGE_PREFIX)


def test_the_unbind_key_space_cannot_be_reached_by_an_endpoint_url() -> None:
    """Both flows share one bounded challenge table, keyed by (agent_id,
    subject). The unbind subject must therefore be something no endpoint can
    ever be, or an attacker could aim a bind challenge at the unbind key and
    grief a pending revocation. It is not a URL the policy would accept, and
    every endpoint that reaches `issue_challenge` has already passed that
    policy — asserted here rather than left as a claim in a comment."""
    with pytest.raises(EndpointPolicyError):
        validate_endpoint_url(UNBIND_SUBJECT)


# ── the replay that the design exists to stop ───────────────────


def test_a_captured_bind_signature_cannot_unbind() -> None:
    owner = Keypair.random()
    bind_nonce, _ = issue_challenge("ext_replay1", ENDPOINT)
    issue_unbind_challenge("ext_replay1")

    captured = _sign(owner, binding_message("ext_replay1", ENDPOINT, bind_nonce))

    assert verify_unbind_challenge("ext_replay1", owner.public_key, captured) is False
    # And the failure cost the owner nothing: the real unbind challenge is
    # untouched, so an attacker cannot use a rejected replay to burn it.
    unbind_nonce, _ = issue_unbind_challenge("ext_replay1")
    assert verify_unbind_challenge("ext_replay1", owner.public_key, _sign_unbind(owner, "ext_replay1", unbind_nonce))


def test_a_bind_signature_cannot_unbind_even_when_the_nonces_are_identical() -> None:
    """The barrier under the barrier. The two challenges normally hold
    different nonces, which alone would defeat the replay — so this test forces
    them to hold the SAME one and shows the domain separator still refuses it.
    Without that, a future change that keyed both flows alike would look safe."""
    owner = Keypair.random()
    nonce, expires_at = issue_challenge("ext_replay2", ENDPOINT)
    eb._challenges[("ext_replay2", UNBIND_SUBJECT)] = (nonce, expires_at)

    captured = _sign(owner, binding_message("ext_replay2", ENDPOINT, nonce))

    assert verify_unbind_challenge("ext_replay2", owner.public_key, captured) is False


def test_a_captured_unbind_signature_cannot_bind() -> None:
    # The reverse direction. An unbind signature names no endpoint, so it must
    # not be able to authorise one.
    owner = Keypair.random()
    issue_challenge("ext_replay3", ENDPOINT)
    unbind_nonce, _ = issue_unbind_challenge("ext_replay3")

    captured = _sign_unbind(owner, "ext_replay3", unbind_nonce)

    assert verify_challenge("ext_replay3", ENDPOINT, owner.public_key, captured) is False


def test_an_unbind_signature_for_one_agent_cannot_unbind_another() -> None:
    # The agent id is in the signed bytes, so a signature cannot be moved
    # sideways to an agent the signer does not own.
    owner = Keypair.random()
    nonce, _ = issue_unbind_challenge("ext_replay4")
    issue_unbind_challenge("ext_replay5")

    captured = _sign_unbind(owner, "ext_replay4", nonce)

    assert verify_unbind_challenge("ext_replay5", owner.public_key, captured) is False


# ── the lifecycle the unbind flow inherits ──────────────────────


def test_the_owner_s_signature_verifies_once_and_only_once() -> None:
    owner = Keypair.random()
    nonce, _ = issue_unbind_challenge("ext_once1")
    signature = _sign_unbind(owner, "ext_once1", nonce)

    assert verify_unbind_challenge("ext_once1", owner.public_key, signature) is True
    # Single use: a proven nonce never verifies twice, so a captured unbind
    # cannot be replayed against a binding the operator made afterwards.
    assert verify_unbind_challenge("ext_once1", owner.public_key, signature) is False


def test_a_sep53_signature_verifies_too() -> None:
    # The path a real wallet takes: StellarWalletsKit delegates signMessage to
    # Freighter, which implements SEP-53. Without this the feature passes CI
    # and fails against the most common Stellar wallet.
    owner = Keypair.random()
    nonce, _ = issue_unbind_challenge("ext_sep53u")

    assert verify_unbind_challenge(
        "ext_sep53u", owner.public_key, _sign_sep53(owner, unbinding_message("ext_sep53u", nonce))
    )


def test_a_signature_from_another_wallet_is_refused() -> None:
    owner = Keypair.random()
    impostor = Keypair.random()
    nonce, _ = issue_unbind_challenge("ext_imp1")

    assert verify_unbind_challenge("ext_imp1", owner.public_key, _sign_unbind(impostor, "ext_imp1", nonce)) is False
    # The honest owner's challenge survives the attempt — only a proven
    # signature consumes a nonce.
    assert verify_unbind_challenge("ext_imp1", owner.public_key, _sign_unbind(owner, "ext_imp1", nonce)) is True


def test_an_unbind_without_a_challenge_is_refused() -> None:
    owner = Keypair.random()
    bogus = base64.b64encode(b"x" * 64).decode("ascii")

    assert verify_unbind_challenge("ext_nochallenge", owner.public_key, bogus) is False


def test_an_expired_unbind_challenge_is_refused() -> None:
    owner = Keypair.random()
    nonce, _ = issue_unbind_challenge("ext_exp1", ttl_seconds=-1)  # already expired at issue

    assert verify_unbind_challenge("ext_exp1", owner.public_key, _sign_unbind(owner, "ext_exp1", nonce)) is False


def test_re_issuing_inside_the_window_returns_the_same_challenge() -> None:
    """The property that makes keying an unbind by the agent id alone safe.
    Without it any anonymous caller could loop the public challenge route and
    permanently stop the real owner completing a revocation, because every
    request would mint a fresh nonce over the one being signed."""
    first = issue_unbind_challenge("ext_idem1")

    assert issue_unbind_challenge("ext_idem1") == first


def test_a_bind_and_an_unbind_challenge_coexist_for_one_agent() -> None:
    """Separate keys, so minting one cannot cancel the other — an operator
    rebinding and revoking in either order is not a race."""
    owner = Keypair.random()
    bind_nonce, _ = issue_challenge("ext_both1", ENDPOINT)
    unbind_nonce, _ = issue_unbind_challenge("ext_both1")

    assert bind_nonce != unbind_nonce
    assert verify_challenge(
        "ext_both1", ENDPOINT, owner.public_key, _sign(owner, binding_message("ext_both1", ENDPOINT, bind_nonce))
    )
    assert verify_unbind_challenge("ext_both1", owner.public_key, _sign_unbind(owner, "ext_both1", unbind_nonce))


def test_unbind_challenges_share_the_one_bounded_table() -> None:
    """The reuse, asserted: the cap that protects a 512 MB instance from the
    public bind route has to cover the public unbind route too, and it does
    because there is one table rather than two."""
    saved = eb._challenges.copy()
    eb._challenges.clear()
    try:
        for i in range(eb.MAX_CHALLENGES + 20):
            issue_unbind_challenge(f"flood_{i}")
        assert len(eb._challenges) == eb.MAX_CHALLENGES
        assert ("flood_0", UNBIND_SUBJECT) not in eb._challenges
        assert (f"flood_{eb.MAX_CHALLENGES + 19}", UNBIND_SUBJECT) in eb._challenges
    finally:
        eb._challenges.clear()
        eb._challenges.update(saved)
