"""The proof that authorises a DISPUTE — the third purpose of one challenge table.

`tests/test_external_binding_spike.py` pins the bind challenge and
`tests/test_unbind_challenge.py` the unbind one. This file pins the dispute
challenge a buyer signs to open a dispute against a step they paid for (story
4.02, ADR 0002), and most of it is about the property the design turns on:
A SIGNATURE FOR ONE PURPOSE MUST NOT VERIFY AS ANOTHER.

That matters more here than anywhere, because a dispute signature moves money:
story 4.03 pays a settler-funded credit for every dispute that is upheld. The
signatures involved are not secrets — all three are handed to public,
unauthenticated routes and stay syntactically valid forever — so if an operator's
bind signature could also open disputes, anyone who ever watched an agent being
bound could mint credits against that agent's jobs. Three distinct domain
separators are what make that impossible, and the tests assert the separation
from every direction rather than trusting that the code paths merely look
different.

The rest covers what the dispute flow INHERITS by reusing `issue_challenge`
rather than forking it — single use, expiry, idempotency inside the window, the
one bounded table — because that inheritance is the reason a third purpose was
cheap, and a future change that forks it should fail here.

Hermetic: pure crypto and an in-process table, no chain and no network.
"""

from __future__ import annotations

import base64

import pytest
from stellar_sdk import Keypair

from app.services import external_binding as eb
from app.services.endpoint_policy import EndpointPolicyError, validate_endpoint_url
from app.services.external_binding import (
    DISPUTE_MESSAGE_PREFIX,
    UNBIND_SUBJECT,
    binding_message,
    dispute_message,
    dispute_subject,
    issue_challenge,
    issue_dispute_challenge,
    issue_unbind_challenge,
    unbinding_message,
    verify_challenge,
    verify_dispute_challenge,
    verify_unbind_challenge,
)

ENDPOINT = "https://operator.example/run"
JOB = "a1b2c3d4e5f60718293a4b5c6d7e8f90"  # 16 bytes of job id, as hex


def _sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def _sign_sep53(keypair: Keypair, message: str) -> str:
    """What Freighter produces: sign(sha256(b"Stellar Signed Message:\\n" + msg))."""
    return base64.b64encode(keypair.sign_message(message.encode("utf-8"))).decode("ascii")


def _sign_dispute(keypair: Keypair, job_id_hex: str, step_index: int, nonce: str) -> str:
    return _sign(keypair, dispute_message(job_id_hex, step_index, nonce))


# ── the message ─────────────────────────────────────────────────


def test_the_dispute_message_is_domain_separated_and_versioned() -> None:
    assert DISPUTE_MESSAGE_PREFIX == "orizon-dispute:v1"
    assert dispute_message(JOB, 2, "cafe") == f"orizon-dispute:v1:{JOB}:2:cafe"


def test_the_dispute_message_names_the_step() -> None:
    # The step decides how much is credited and which agent 4.04 rates, so a
    # signature that named only the job could be replayed against a different,
    # more expensive step of the same workflow.
    assert dispute_message(JOB, 0, "cafe") != dispute_message(JOB, 1, "cafe")


def test_the_three_messages_never_collide() -> None:
    # Not "they happen to differ" — three different domains, so no choice of
    # ids, endpoint, step or nonce can make any one equal another.
    dispute = dispute_message(JOB, 0, "cafe")
    assert not dispute.startswith(eb.BINDING_MESSAGE_PREFIX)
    assert not dispute.startswith(eb.UNBINDING_MESSAGE_PREFIX)
    assert not binding_message(JOB, ENDPOINT, "cafe").startswith(DISPUTE_MESSAGE_PREFIX)
    assert not unbinding_message(JOB, "cafe").startswith(DISPUTE_MESSAGE_PREFIX)


# ── the key space the three purposes share ──────────────────────


def test_the_dispute_key_space_cannot_be_reached_by_an_endpoint_url() -> None:
    """All three flows share one bounded table keyed by (scope, subject), so a
    dispute subject must be something no endpoint can ever be — otherwise a bind
    challenge could be aimed at a pending dispute's key and grief it. It is not
    a URL the policy accepts, and every endpoint that reaches `issue_challenge`
    has already passed that policy."""
    with pytest.raises(EndpointPolicyError):
        validate_endpoint_url(dispute_subject(0))


def test_the_dispute_key_space_cannot_collide_with_the_unbind_one() -> None:
    # The unbind subject is a bare prefix; a dispute subject always carries a
    # step, and a different domain, so the two can never be the same string.
    assert dispute_subject(0) != UNBIND_SUBJECT
    assert not dispute_subject(0).startswith(UNBIND_SUBJECT)


def test_each_step_gets_its_own_challenge() -> None:
    """Keyed per STEP, for the reason a bind is keyed per endpoint: the
    challenge authorises exactly what the message names. Disputing step 0 must
    not cancel a challenge the buyer is mid-way through signing for step 1."""
    first, _ = issue_dispute_challenge(JOB, 0)
    second, _ = issue_dispute_challenge(JOB, 1)

    assert first != second
    assert issue_dispute_challenge(JOB, 0)[0] == first  # untouched by the second mint


# ── the replay the design exists to stop ────────────────────────


def test_a_captured_bind_signature_cannot_open_a_dispute() -> None:
    payer = Keypair.random()
    bind_nonce, _ = issue_challenge(JOB, ENDPOINT)
    issue_dispute_challenge(JOB, 0)

    captured = _sign(payer, binding_message(JOB, ENDPOINT, bind_nonce))

    assert verify_dispute_challenge(JOB, 0, payer.public_key, captured) is False
    # And the attempt cost the buyer nothing: the real challenge is untouched.
    nonce, _ = issue_dispute_challenge(JOB, 0)
    assert verify_dispute_challenge(JOB, 0, payer.public_key, _sign_dispute(payer, JOB, 0, nonce)) is True


def test_a_bind_signature_cannot_open_a_dispute_even_when_the_nonces_are_identical() -> None:
    """The barrier under the barrier. The two challenges normally hold different
    nonces, which alone would defeat the replay — so this forces them to hold the
    SAME one and shows the domain separator still refuses it. Without this, a
    later change that keyed the flows alike would look safe."""
    payer = Keypair.random()
    nonce, expires_at = issue_challenge("job_same", ENDPOINT)
    eb._challenges[("job_same", dispute_subject(0))] = (nonce, expires_at)

    captured = _sign(payer, binding_message("job_same", ENDPOINT, nonce))

    assert verify_dispute_challenge("job_same", 0, payer.public_key, captured) is False


def test_a_captured_unbind_signature_cannot_open_a_dispute() -> None:
    payer = Keypair.random()
    unbind_nonce, _ = issue_unbind_challenge(JOB)
    issue_dispute_challenge(JOB, 3)

    captured = _sign(payer, unbinding_message(JOB, unbind_nonce))

    assert verify_dispute_challenge(JOB, 3, payer.public_key, captured) is False


def test_a_captured_dispute_signature_cannot_bind_or_unbind() -> None:
    # The reverse direction, which is the one that would cost an operator their
    # endpoint: a buyer's dispute signature must not revoke the agent they are
    # disputing, nor point it at a host they control.
    payer = Keypair.random()
    issue_challenge("job_rev", ENDPOINT)
    issue_unbind_challenge("job_rev")
    nonce, _ = issue_dispute_challenge("job_rev", 0)

    captured = _sign_dispute(payer, "job_rev", 0, nonce)

    assert verify_challenge("job_rev", ENDPOINT, payer.public_key, captured) is False
    assert verify_unbind_challenge("job_rev", payer.public_key, captured) is False


def test_a_dispute_signature_for_one_step_cannot_dispute_another() -> None:
    # The step is in the signed bytes, so a signature over a cheap step cannot
    # be moved sideways to an expensive one in the same workflow.
    payer = Keypair.random()
    nonce, _ = issue_dispute_challenge("job_steps", 0)
    issue_dispute_challenge("job_steps", 1)

    captured = _sign_dispute(payer, "job_steps", 0, nonce)

    assert verify_dispute_challenge("job_steps", 1, payer.public_key, captured) is False


def test_a_dispute_signature_for_one_job_cannot_dispute_another() -> None:
    payer = Keypair.random()
    nonce, _ = issue_dispute_challenge("job_one", 0)
    issue_dispute_challenge("job_two", 0)

    captured = _sign_dispute(payer, "job_one", 0, nonce)

    assert verify_dispute_challenge("job_two", 0, payer.public_key, captured) is False


def test_a_signature_from_another_wallet_is_refused() -> None:
    payer = Keypair.random()
    impostor = Keypair.random()
    nonce, _ = issue_dispute_challenge("job_imp", 0)

    assert (
        verify_dispute_challenge("job_imp", 0, payer.public_key, _sign_dispute(impostor, "job_imp", 0, nonce)) is False
    )
    # The honest buyer's challenge survives it — only a proven signature
    # consumes a nonce, so an impostor cannot burn a dispute in progress.
    assert verify_dispute_challenge("job_imp", 0, payer.public_key, _sign_dispute(payer, "job_imp", 0, nonce)) is True
