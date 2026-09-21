"""The rules that decide whether a dispute may exist (app/services/dispute_svc.py).

Story 4.02, ADR 0002. Every credit story 4.03 pays starts as a record written
here, so this file is the one that has to be paranoid: one test per rule, plus
the two properties that hold across all of them — a dispute writes NOTHING
on-chain, and a caller who cannot prove they are the buyer learns nothing about
the workflow beyond the fact that it settled (which the escrow's `charged` event
already says in public).

Hermetic: the in-memory dispute store, an in-process challenge table, and real
ed25519 keys. No chain, no network, no database. The store singleton and the
challenge table are reset between tests so nothing leaks from one rule to the
next.
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timezone

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
from app.services import external_binding as eb
from app.services.dispute_store import SettlementRecord, SettlementStep
from app.services.dispute_svc import DisputeError, dispute_message

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_seo", agent_name="SEO Brief", price_usdc=0.07, delivered=True),
)


@pytest.fixture(autouse=True)
def _fresh_state():
    """A store and a challenge table per test — both are process singletons."""
    dispute_store._store = None
    eb._challenges.clear()
    yield
    dispute_store._store = None
    eb._challenges.clear()


def _sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def _seed(
    payer: str,
    *,
    job: str = JOB,
    task: str = TASK,
    steps: tuple[SettlementStep, ...] = STEPS,
    settled_usdc: float = 0.12,
    window_seconds: float = 3600.0,
) -> SettlementRecord:
    """Record a settlement the way `execution_svc` will when a workflow seals."""
    now = time.time()
    record = SettlementRecord(
        task_id=task,
        payer=payer,
        auth_id_hex="ab" * 16,
        job_id_hex=job,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=settled_usdc,
        steps=steps,
        settled_at=now,
        window_closes_at=now + window_seconds,
    )
    asyncio.run(dispute_store.get_dispute_store().record_settlement(record))
    return record


def _open(
    payer: Keypair,
    *,
    job: str = JOB,
    step: int = 0,
    reason: str = "the draft ignored half the brief",
    nonce: str | None = None,
    signature: str | None = None,
    claimed_payer: str | None = None,
):
    """The whole flow a router performs: mint a challenge, sign it, open."""
    if nonce is None:
        nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(job, step))
    if signature is None:
        signature = _sign(payer, dispute_message(job, step, nonce))
    return asyncio.run(
        dispute_svc.open_dispute(
            job_id_hex=job,
            step_index=step,
            reason=reason,
            payer=claimed_payer or payer.public_key,
            nonce=nonce,
            signature_b64=signature,
        )
    )


# ── the happy path ──────────────────────────────────────────────


def test_the_payer_opens_a_dispute_against_a_settled_step() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)

    record = _open(payer, step=1)

    assert record.status == "open"
    assert record.id.startswith("dsp_")
    assert record.job_id_hex == JOB
    assert record.task_id == TASK
    assert record.step_index == 1
    assert record.agent_id == "agt_seo"  # the agent that delivered THAT step
    assert record.payer == payer.public_key
    assert record.reason == "the draft ignored half the brief"
    # The step's own settled price, and the full credit the default policy
    # promises for it — not the workflow's 0.12 total.
    assert record.charged_usdc == 0.07
    assert record.creditable_usdc == 0.07
    assert record.resolved_at is None and record.refund_tx is None and record.rating_tx is None
    assert record.opened_at <= time.time()


def test_the_credit_follows_the_configured_fraction(monkeypatch) -> None:
    """`creditable_usdc` is frozen at opening time from the policy in force,
    so a later change to the setting cannot rewrite what a buyer was shown."""
    payer = Keypair.random()
    _seed(payer.public_key)
    monkeypatch.setattr(settings, "dispute_credited_fraction", 0.5)

    record = _open(payer, step=1)

    assert record.creditable_usdc == refund_svc.credited_amount_usdc(0.07, 0.5) == 0.035
    monkeypatch.setattr(settings, "dispute_credited_fraction", 1.0)
    assert asyncio.run(dispute_svc.get_dispute(record.id)).creditable_usdc == 0.035


def test_opening_a_dispute_writes_nothing_on_chain(monkeypatch) -> None:
    """The boundary ADR 0002 draws: a dispute is a CLAIM. 4.03 pays the credit
    and 4.04 writes the rating; 4.02 must not submit a transaction, must not
    read the chain, and must not touch reputation — so every way out of this
    process is booby-trapped and the happy path still runs."""

    def _boom(*args, **kwargs):
        raise AssertionError("story 4.02 must not touch the chain")

    for name in (
        "simulate_read",
        "invoke_with_server_key",
        "invoke_with_server_key_async",
        "submit_rating",
        "submit_rating_async",
        "submit_signed_xdr",
        "submit_signed_xdr_async",
        "build_invoke_xdr",
        "signer_public_key",
        "_server",
    ):
        monkeypatch.setattr(sc, name, _boom)
    monkeypatch.setattr(refund_svc, "execute_refund", _boom)
    monkeypatch.setattr(refund_svc, "record_dispute_rating", _boom)

    payer = Keypair.random()
    _seed(payer.public_key)

    assert _open(payer).status == "open"


def test_a_successful_dispute_consumes_its_challenge() -> None:
    """Single use, end to end: the proof that opened this dispute cannot be
    replayed, and the record is what the buyer is answered with afterwards."""
    payer = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))

    record = _open(payer, nonce=nonce)

    assert eb.dispute_challenge_is_live(JOB, 0, nonce) is False
    assert asyncio.run(dispute_svc.get_dispute(record.id)) == record


# ── the read surface four other lanes code against ──────────────


def test_the_read_surface_answers_by_id_and_by_task() -> None:
    payer = Keypair.random()
    settlement = _seed(payer.public_key)

    first = _open(payer, step=0)
    second = _open(payer, step=1)

    assert asyncio.run(dispute_svc.get_dispute(first.id)) == first
    assert asyncio.run(dispute_svc.get_dispute("dsp_nosuchdispute")) is None
    assert asyncio.run(dispute_svc.list_for_task(TASK)) == (first, second)
    assert asyncio.run(dispute_svc.list_for_task("tsk_other")) == ()
    assert asyncio.run(dispute_svc.settlement_for_task(TASK)) == settlement
    assert asyncio.run(dispute_svc.settlement_for_task("tsk_other")) is None


# ── rule: the payer proves themselves ───────────────────────────


def test_a_signature_from_another_wallet_is_refused() -> None:
    payer = Keypair.random()
    impostor = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))

    with pytest.raises(DisputeError) as refused:
        _open(impostor, nonce=nonce, claimed_payer=payer.public_key)

    assert refused.value.code == "not_the_payer"
    assert refused.value.status_code == 403
    assert refused.value.existing is None
    # Nothing was written, and the honest buyer's challenge survived the
    # attempt — only a proven signature consumes a nonce, so an impostor
    # cannot burn a dispute the real buyer is mid-way through.
    assert asyncio.run(dispute_svc.list_for_task(TASK)) == ()
    assert _open(payer, nonce=nonce).status == "open"


def test_a_caller_claiming_someone_else_s_address_is_refused() -> None:
    """The supplied payer is checked against the RECORDED one before any
    crypto runs, so a caller who signs correctly for their own wallet cannot
    open a dispute on somebody else's workflow."""
    payer = Keypair.random()
    stranger = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))

    with pytest.raises(DisputeError) as refused:
        _open(stranger, nonce=nonce)

    assert refused.value.code == "not_the_payer"
    assert refused.value.status_code == 403
    # The address compare happens BEFORE the signature check, so the buyer's
    # challenge is still there to use.
    assert eb.dispute_challenge_is_live(JOB, 0, nonce) is True


def test_a_wrong_signature_reveals_nothing_about_the_workflow() -> None:
    """The signature gate stands in front of every private fact. This job's
    window has closed and its step was never delivered, and a caller who cannot
    prove they are the buyer is told neither — the refusal is the same code,
    status and message they would get against a perfectly healthy job."""
    payer = Keypair.random()
    impostor = Keypair.random()
    _seed(payer.public_key)
    _seed(
        payer.public_key,
        job="dead" * 8,
        task="tsk_closed",
        steps=(SettlementStep(step_index=0, agent_id="agt_x", agent_name=None, price_usdc=0.05, delivered=False),),
        window_seconds=-10.0,
    )

    with pytest.raises(DisputeError) as healthy:
        _open(impostor, claimed_payer=payer.public_key)
    with pytest.raises(DisputeError) as damaged:
        _open(impostor, job="dead" * 8, claimed_payer=payer.public_key)

    assert (healthy.value.code, healthy.value.status_code) == ("not_the_payer", 403)
    assert (damaged.value.code, damaged.value.status_code) == ("not_the_payer", 403)
    assert healthy.value.message == damaged.value.message


@pytest.mark.parametrize(
    ("signature", "why"),
    [
        ("not base64 at all!!", "not base64"),
        (base64.b64encode(b"x" * 32).decode("ascii"), "32 bytes, not 64"),
        ("A" * 300, "longer than any real signature"),
    ],
)
def test_a_malformed_signature_is_refused_before_anything_is_touched(signature: str, why: str) -> None:
    payer = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))

    with pytest.raises(DisputeError) as refused:
        _open(payer, nonce=nonce, signature=signature)

    assert refused.value.code == "signature_malformed", why
    assert refused.value.status_code == 400
    assert eb.dispute_challenge_is_live(JOB, 0, nonce) is True  # a client bug costs the buyer nothing


def test_a_dispute_without_a_challenge_is_refused() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)
    never_issued = "0" * 32

    with pytest.raises(DisputeError) as refused:
        _open(payer, nonce=never_issued, signature=_sign(payer, dispute_message(JOB, 0, never_issued)))

    assert refused.value.code == "challenge_expired"
    assert refused.value.status_code == 400


def test_an_expired_challenge_is_refused_and_says_to_ask_for_another() -> None:
    """Distinguished from a bad signature on purpose: a buyer who spent a
    minute in a wallet dialog must be told to re-mint, not sent looking for a
    problem with their wallet."""
    payer = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = eb.issue_dispute_challenge(JOB, 0, ttl_seconds=-1)  # already expired at issue

    with pytest.raises(DisputeError) as refused:
        _open(payer, nonce=nonce)

    assert refused.value.code == "challenge_expired"
    assert "new one" in refused.value.message


def test_a_nonce_that_is_not_the_one_issued_is_refused() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)
    issued, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))
    other, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 1))

    # A wrong shape, a non-ASCII string the length of a real nonce (which the
    # constant-time compare cannot be handed), then another step's LIVE nonce.
    for nonce in ("deadbeef", "é" * 32, other):
        with pytest.raises(DisputeError) as refused:
            _open(payer, step=0, nonce=nonce)
        assert refused.value.code == "challenge_expired"
    assert eb.dispute_challenge_is_live(JOB, 0, issued) is True


# ── rule: the settlement exists ─────────────────────────────────


def test_a_job_that_never_settled_cannot_be_disputed() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)
    unknown = "ff" * 16
    nonce = "0" * 32

    with pytest.raises(DisputeError) as refused:
        _open(payer, job=unknown, nonce=nonce, signature=_sign(payer, dispute_message(unknown, 0, nonce)))

    assert refused.value.code == "unknown_job"
    assert refused.value.status_code == 404


def test_no_challenge_is_minted_for_a_job_or_step_that_cannot_be_disputed() -> None:
    """The mint refuses what it cannot key, so the ONE bounded challenge table
    that bind, unbind and disputes share cannot be filled with invented job ids
    — and the bind route's cap protects all three."""
    payer = Keypair.random()
    _seed(payer.public_key)

    with pytest.raises(DisputeError) as unknown:
        asyncio.run(dispute_svc.issue_dispute_challenge("ff" * 16, 0))
    with pytest.raises(DisputeError) as no_step:
        asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 7))

    assert (unknown.value.code, unknown.value.status_code) == ("unknown_job", 404)
    assert (no_step.value.code, no_step.value.status_code) == ("step_not_settled", 409)
    assert len(eb._challenges) == 0


# ── rule: the window is open ────────────────────────────────────


def test_a_dispute_after_the_window_closed_is_refused_and_says_when() -> None:
    payer = Keypair.random()
    settlement = _seed(payer.public_key, window_seconds=-60.0)
    closed_at = datetime.fromtimestamp(settlement.window_closes_at, timezone.utc).isoformat(timespec="seconds")

    with pytest.raises(DisputeError) as refused:
        _open(payer)

    assert refused.value.code == "dispute_window_closed"
    assert refused.value.status_code == 409
    # The buyer is told the deadline they missed, not just that they missed one.
    assert closed_at in refused.value.message


def test_the_window_is_judged_on_the_stamped_value_not_the_setting(monkeypatch) -> None:
    """The promise is what the settlement recorded. Tuning the setting later
    must not reopen a window that closed, nor close one that is still open —
    `window_closes_at` is stamped per settlement for exactly this reason."""
    payer = Keypair.random()
    _seed(payer.public_key, window_seconds=-60.0)
    _seed(payer.public_key, job="beef" * 8, task="tsk_open", window_seconds=3600.0)

    monkeypatch.setattr(settings, "dispute_window_seconds", 365 * 86_400.0)
    with pytest.raises(DisputeError) as still_closed:
        _open(payer)
    assert still_closed.value.code == "dispute_window_closed"

    monkeypatch.setattr(settings, "dispute_window_seconds", 0.0)
    assert _open(payer, job="beef" * 8).status == "open"  # still open, as promised


# ── rule: the step was settled ──────────────────────────────────


def test_a_step_the_workflow_never_had_cannot_be_disputed() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)
    # Minted straight from the challenge module: the service's own mint refuses
    # this step, so reaching `open_dispute` with one takes a hand-made nonce.
    nonce, _ = eb.issue_dispute_challenge(JOB, 9)

    with pytest.raises(DisputeError) as refused:
        _open(payer, step=9, nonce=nonce)

    assert refused.value.code == "step_not_settled"
    assert refused.value.status_code == 409
    assert "no step 9" in refused.value.message


def test_a_step_that_produced_no_output_cannot_be_disputed() -> None:
    """A failed step was never part of what the buyer paid for, so there is
    nothing to credit — the refund is a credit of a CHARGE, not compensation."""
    payer = Keypair.random()
    _seed(
        payer.public_key,
        steps=(SettlementStep(step_index=0, agent_id="agt_x", agent_name=None, price_usdc=0.05, delivered=False),),
    )

    with pytest.raises(DisputeError) as refused:
        _open(payer)

    assert refused.value.code == "step_not_settled"
    assert refused.value.status_code == 409


# ── rule: money actually moved ──────────────────────────────────


def test_a_workflow_that_charged_nothing_cannot_be_disputed() -> None:
    """`settled_usdc` is what moved on-chain, not the plan's estimate. With no
    transfer there is nothing to credit back, and a credit would be a
    withdrawal from the platform wallet rather than a remedy."""
    payer = Keypair.random()
    _seed(payer.public_key, settled_usdc=0.0)

    with pytest.raises(DisputeError) as refused:
        _open(payer)

    assert refused.value.code == "nothing_was_charged"
    assert refused.value.status_code == 409


def test_a_free_step_cannot_be_disputed() -> None:
    payer = Keypair.random()
    _seed(
        payer.public_key,
        steps=(SettlementStep(step_index=0, agent_id="agt_x", agent_name=None, price_usdc=0.0, delivered=True),),
    )

    with pytest.raises(DisputeError) as refused:
        _open(payer)

    assert refused.value.code == "nothing_was_charged"
    assert "free" in refused.value.message


# ── rule: one dispute per step ──────────────────────────────────


def test_a_second_dispute_returns_the_first_one_unchanged() -> None:
    """A step is credited once, so the second press of the button is answered
    with the dispute the buyer already has — not an error they cannot act on,
    and not a second record 4.03 would pay twice."""
    payer = Keypair.random()
    _seed(payer.public_key)
    original = _open(payer, reason="the draft ignored half the brief")

    with pytest.raises(DisputeError) as refused:
        _open(payer, reason="a different complaint entirely")

    assert refused.value.code == "duplicate_dispute"
    assert refused.value.status_code == 409
    assert refused.value.existing == original  # id, reason, timestamps: untouched
    assert asyncio.run(dispute_svc.list_for_task(TASK)) == (original,)


def test_a_race_is_answered_like_any_other_duplicate(monkeypatch) -> None:
    """Two requests that both pass the pre-check still meet in the store, which
    is the only check that holds under a race — so the store raising is the
    path under test here, forced rather than raced. The loser must get the
    winner's dispute back: a 500 would tell a buyer their dispute failed when
    one exists, and they would have spent their challenge finding out."""
    payer = Keypair.random()
    _seed(payer.public_key)
    original = _open(payer)

    async def _lost_the_race(record):
        raise dispute_store.DuplicateDisputeError(original)

    monkeypatch.setattr(dispute_store.get_dispute_store(), "open_dispute", _lost_the_race)

    with pytest.raises(DisputeError) as refused:
        _open(payer, step=1)  # a step with no dispute, so the pre-check passes

    assert refused.value.code == "duplicate_dispute"
    assert refused.value.status_code == 409
    assert refused.value.existing == original


# ── rule: a mandatory, bounded, cleaned reason ──────────────────


def test_a_dispute_must_say_what_was_wrong() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)

    with pytest.raises(DisputeError) as refused:
        _open(payer, reason="   \t  ")

    assert refused.value.code == "reason_required"
    assert refused.value.status_code == 422
    assert asyncio.run(dispute_svc.list_for_task(TASK)) == ()


def test_an_empty_reason_is_refused_before_the_proof_is_spent() -> None:
    """Why the reason is checked FIRST. It is the only refusal the buyer can
    fix and retry, and verifying the signature consumes the challenge — so a
    buyer refused for an empty reason has to be able to send the very same
    nonce and signature again with the text filled in, rather than making a
    second trip through their wallet."""
    payer = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))
    signature = _sign(payer, dispute_message(JOB, 0, nonce))

    with pytest.raises(DisputeError):
        _open(payer, reason="", nonce=nonce, signature=signature)

    assert eb.dispute_challenge_is_live(JOB, 0, nonce) is True
    assert _open(payer, reason="the draft ignored the brief", nonce=nonce, signature=signature).status == "open"


def test_a_reason_of_control_characters_alone_is_no_reason() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)

    with pytest.raises(DisputeError) as refused:
        _open(payer, reason="\x00\x1b\x07")

    assert refused.value.code == "reason_required"


def test_control_characters_are_stripped_from_a_stored_reason() -> None:
    """The reason is read back into an API response, shown in the console and
    quoted in the receipt. Tab and newline survive — a buyer may write a
    paragraph — but nothing that forges structure does."""
    payer = Keypair.random()
    _seed(payer.public_key)

    record = _open(payer, reason="step one\nwas \x00wrong\x1b[31m and late")

    assert "\x00" not in record.reason and "\x1b" not in record.reason
    assert "\n" in record.reason
    assert record.reason.startswith("step one")


def test_a_very_long_reason_is_clamped() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)

    record = _open(payer, reason="x" * 5_000)

    assert len(record.reason) <= dispute_svc.MAX_REASON_CHARS + len(" …[truncated]")
    assert record.reason.endswith("[truncated]")


# ── the refusal type itself ─────────────────────────────────────


def test_the_refusal_carries_the_code_status_and_record_the_api_answers_with() -> None:
    """The shape four other lanes code against, including the keyword form the
    router uses for a duplicate, and the message a code falls back to — the
    same wording `main._error_envelope` derives from a snake token."""
    bare = DisputeError(code="duplicate_dispute", status_code=409, existing=None)

    assert (bare.code, bare.status_code, bare.existing) == ("duplicate_dispute", 409, None)
    assert bare.message == "duplicate dispute"
    assert str(bare) == "duplicate dispute"

    spoken = DisputeError("unknown_job", "no settled workflow with that job id", 404)
    assert spoken.message == "no settled workflow with that job id"
