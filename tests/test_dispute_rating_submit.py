"""Story 4.04 — writing an upheld dispute's rating (app/services/dispute_rating.py).

The dispute's consequence for the agent is one `ReputationLedger.submit`, and
every argument to it is load-bearing: the DERIVED id is the only key the
settler's own auto-rating does not already hold, the weight has to be the one
the settler used or the two ratings of one step would carry different evidence,
and `kind="dispute"` is the only thing that moves `dispute_rate_bps`. So the
arguments are pinned exactly, and then every answer the chain can give is
pinned to its outcome:

  - SUCCESS, FAILED, TIMEOUT and REPLAY are four distinct outcomes, and anything
    whose fate is unknown lands on TIMEOUT — never FAILED;
  - only the contract's `Replay` is REPLAY, and `Unauthorized` is logged as the
    deployment misconfiguration it is rather than as a verdict on the dispute;
  - every line that is not a success names the dispute, the sealed job, the
    derived id, the agent, the payer, the weight and the rating — and none of
    them carries the signing key.

Hermetic: dataclass records built in-process, the stellar client monkeypatched.
No chain, no network, no database.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.config import settings
from app.services import reputation_svc
from app.services.dispute_rating import DISPUTE_RATING, dispute_job_id, submit_dispute_rating
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
DISPUTE_ID = "dsp_deadbeefdeadbeef"
PAYER = Keypair.random().public_key
SIGNING_SECRET = Keypair.random().secret

# One agent serving two steps of the same job: the case 4.01's job-only
# derivation collided on, so the fixtures carry it by default.
STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.07, delivered=True),
)
# The settler's weight for step 1: 0.07 USDC in stroops.
WEIGHT = 700_000
DERIVED = dispute_job_id(bytes.fromhex(JOB), 1).hex()


def _settlement(*, steps: tuple[SettlementStep, ...] = STEPS) -> SettlementRecord:
    """A settlement as `execution_svc` records one when a workflow seals."""
    return SettlementRecord(
        task_id=TASK,
        payer=PAYER,
        auth_id_hex="ab" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=0.12,
        steps=steps,
        settled_at=1_700_000_000.0,
        window_closes_at=1_700_086_400.0,
    )


def _dispute(*, step_index: int = 1, job_id_hex: str = JOB) -> DisputeRecord:
    """A dispute whose refund has landed — the only state a rating follows."""
    return DisputeRecord(
        id=DISPUTE_ID,
        job_id_hex=job_id_hex,
        task_id=TASK,
        step_index=step_index,
        agent_id="agt_writer",
        payer=PAYER,
        reason="the brief was empty",
        status="credited",
        charged_usdc=0.07,
        creditable_usdc=0.07,
        opened_at=1_700_000_100.0,
        refund_tx="tx_refund",
    )


def _fake_submit(monkeypatch, outcome: dict | BaseException) -> list[dict]:
    """Stand in for `sc.submit_rating_async`, recording every submit.

    Patched at the stellar client, so the call is made exactly as production
    makes it — positional, through the module attribute.
    """
    calls: list[dict] = []

    async def _submit(agent_id, job_id, rating, weight, payer, kind) -> dict:
        calls.append(
            {"agent_id": agent_id, "job_id": job_id, "rating": rating, "weight": weight, "payer": payer, "kind": kind}
        )
        # BaseException, not Exception: a cancel is one of the cases under test.
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sc, "submit_rating_async", _submit)
    return calls


def _rate(dispute: DisputeRecord | None = None, settlement: SettlementRecord | None = None):
    return asyncio.run(submit_dispute_rating(dispute or _dispute(), settlement or _settlement()))


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.services.dispute_rating" and r.levelno == level]


def _names_every_fact(message: str) -> bool:
    """Whether a line carries what reconciling a dispute rating starts from."""
    facts = (DISPUTE_ID, JOB, DERIVED, "agt_writer", PAYER, f"weight {WEIGHT}", f"rating {DISPUTE_RATING}")
    return all(fact in message for fact in facts)


def test_the_submit_carries_the_derived_id_the_settlers_weight_and_the_payer(monkeypatch) -> None:
    calls = _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    _rate()

    assert calls == [
        {
            "agent_id": "agt_writer",
            "job_id": dispute_job_id(bytes.fromhex(JOB), 1),
            "rating": DISPUTE_RATING,
            "weight": WEIGHT,
            "payer": PAYER,
            "kind": "dispute",
        }
    ]
    # The sealed id is the one key this rating can never land under: the
    # settler's auto-rating already holds it.
    assert calls[0]["job_id"] != bytes.fromhex(JOB)


def test_the_weight_is_the_settlers_helper_over_the_settled_step_price(monkeypatch) -> None:
    """D2: the same helper the settler weights its own rating with, fed the
    settled step's price — so a change to the weighting rule moves both."""
    seen: list[float] = []

    def _weight(price_usdc: float) -> int:
        seen.append(price_usdc)
        return 4_321

    monkeypatch.setattr(reputation_svc, "rating_weight_stroops", _weight)
    calls = _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    outcome = _rate()

    assert seen == [0.07]
    assert calls[0]["weight"] == outcome.weight_stroops == 4_321


def test_two_steps_by_one_agent_rate_under_two_ids(monkeypatch) -> None:
    """The collision 4.01's derivation had: the second step's rating would have
    been refused as a replay of the first."""
    calls = _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    _rate(_dispute(step_index=0))
    _rate(_dispute(step_index=1))

    assert calls[0]["agent_id"] == calls[1]["agent_id"]
    assert calls[0]["job_id"] != calls[1]["job_id"]
    assert calls[0]["weight"] == 500_000 and calls[1]["weight"] == WEIGHT


def test_the_rating_is_below_what_the_settler_gives_a_non_delivery() -> None:
    """An upheld dispute must score beneath honest non-delivery (see the
    constant's comment), measured against what the settler actually awards."""
    non_delivery, _ = reputation_svc.synthetic_rating(None, 0.07)
    assert 0 < DISPUTE_RATING < non_delivery


def test_a_landed_rating_is_a_success_with_one_info_line(monkeypatch, caplog) -> None:
    _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    with caplog.at_level(logging.INFO, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash) == ("SUCCESS", "rating_tx")
    # The DERIVED id — the one a reviewer finds on Stellar Expert.
    assert (outcome.job_id_hex, outcome.rating, outcome.weight_stroops) == (DERIVED, DISPUTE_RATING, WEIGHT)
    records = [r for r in caplog.records if r.name == "app.services.dispute_rating"]
    assert [r.levelno for r in records] == [logging.INFO], "a success is exactly one INFO line"
    assert "rating_tx" in records[0].getMessage() and _names_every_fact(records[0].getMessage())


def test_a_ledger_failed_transaction_is_failed_and_keeps_its_hash(monkeypatch, caplog) -> None:
    """FAILED means the ledger rejected the transaction itself: nothing was
    written. The hash is kept, because a transaction existed."""
    _fake_submit(monkeypatch, {"status": "FAILED", "hash": "failed_tx"})

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash, outcome.job_id_hex) == ("FAILED", "failed_tx", DERIVED)
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any("nothing was written" in m and "failed_tx" in m and _names_every_fact(m) for m in msgs), (
        f"a failed rating was not logged with its context: {msgs}"
    )


def test_a_timed_out_rating_is_a_timeout_and_keeps_its_hash(monkeypatch, caplog) -> None:
    """Submitted and unconfirmed: it may still land, and the in-flight hash is
    how anyone finds out."""
    _fake_submit(monkeypatch, {"status": "timeout", "hash": "inflight_tx"})

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash, outcome.job_id_hex) == ("TIMEOUT", "inflight_tx", DERIVED)
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any("MAY STILL LAND" in m and "inflight_tx" in m and _names_every_fact(m) for m in msgs), (
        f"an unconfirmed rating was not logged for reconciliation: {msgs}"
    )


def test_a_success_without_a_hash_is_a_timeout(monkeypatch) -> None:
    """No receipt is no proof it landed — the unknown bucket, not SUCCESS."""
    _fake_submit(monkeypatch, {"status": "SUCCESS"})
    outcome = _rate()
    assert (outcome.status, outcome.tx_hash) == ("TIMEOUT", None)


def test_an_unrecognised_or_missing_status_is_a_timeout(monkeypatch) -> None:
    _fake_submit(monkeypatch, {"status": "NOT_FOUND", "hash": "maybe_tx"})
    outcome = _rate()
    assert (outcome.status, outcome.tx_hash) == ("TIMEOUT", "maybe_tx")

    _fake_submit(monkeypatch, {"hash": "maybe_tx"})
    assert _rate().status == "TIMEOUT"


def _refused(code: int) -> sc.ContractError:
    """A submit the ledger refused at simulation, as the client raises it."""
    return sc.ContractError(f"prepare failed: HostError: Error(Contract, #{code}) …", code)


def test_a_replay_is_replay_with_no_hash(monkeypatch, caplog) -> None:
    """The one refusal that can mean success: an earlier attempt under this
    derived id landed. Refused at simulation, so no transaction and no hash —
    and not FAILED, which would hide that the rating may be on-chain."""
    _fake_submit(monkeypatch, _refused(7))

    with caplog.at_level(logging.WARNING, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash, outcome.job_id_hex) == ("REPLAY", None, DERIVED)
    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any("Replay" in m and _names_every_fact(m) for m in msgs), f"a replay was not logged: {msgs}"


def test_unauthorized_is_failed_and_named_as_a_scorer_misconfiguration(monkeypatch, caplog) -> None:
    """The misconfiguration that once left a ledger at zero ratings in silence:
    the line has to say it is the deployment, not the dispute, and name the
    ledger and the fix."""
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CLEDGERUNDERTEST")
    _fake_submit(monkeypatch, _refused(1))

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash) == ("FAILED", None)
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(
        "Unauthorized" in m
        and "NOT the Scorer" in m
        and "CLEDGERUNDERTEST" in m
        and "set_scorer" in m
        and "misconfiguration" in m
        and _names_every_fact(m)
        for m in msgs
    ), f"Unauthorized was not logged as a scorer misconfiguration: {msgs}"


@pytest.mark.parametrize(
    ("code", "name"),
    [(2, "NotFound"), (100, "OutOfRange"), (42, "contract error #42")],
)
def test_any_other_refusal_is_failed_and_logged_by_name(monkeypatch, caplog, code: int, name: str) -> None:
    """Refused at simulation, so nothing was submitted — FAILED, not the unknown
    bucket — and logged by the contract's own name, never blamed on the signer."""
    _fake_submit(monkeypatch, _refused(code))

    with caplog.at_level(logging.WARNING, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash, outcome.job_id_hex) == ("FAILED", None, DERIVED)
    records = _records(caplog, logging.ERROR)
    assert any(name in r.getMessage() and _names_every_fact(r.getMessage()) for r in records), (
        f"the refusal was not logged by name: {[r.getMessage() for r in records]}"
    )
    assert not any("Scorer" in r.getMessage() for r in caplog.records), "only Unauthorized is the scorer's fault"
    assert _records(caplog, logging.WARNING) == [], "only Replay is a warning"


def test_a_raising_submit_is_a_timeout_logged_with_its_traceback(monkeypatch, caplog) -> None:
    """A raise can happen either side of the submission and nothing in it says
    which, so the rating's fate is unknown — TIMEOUT, never FAILED."""
    _fake_submit(monkeypatch, RuntimeError("soroban rpc unreachable"))

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        outcome = _rate()

    assert (outcome.status, outcome.tx_hash, outcome.job_id_hex) == ("TIMEOUT", None, DERIVED)
    records = _records(caplog, logging.ERROR)
    assert any(
        "MAY HAVE LANDED" in r.getMessage()
        and "soroban rpc unreachable" in r.getMessage()
        and _names_every_fact(r.getMessage())
        and r.exc_info is not None
        for r in records
    ), f"the raise was not logged with its context and traceback: {[r.getMessage() for r in records]}"


def test_cancellation_mid_submit_is_logged_and_reraised(monkeypatch, caplog) -> None:
    """A deploy-triggered cancel can land between the submit and its
    confirmation, and CancelledError is a BaseException the generic handler
    never sees. The reconstruction line fires, and the cancel is not swallowed
    into an outcome."""
    _fake_submit(monkeypatch, asyncio.CancelledError())

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        with pytest.raises(asyncio.CancelledError):
            _rate()

    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any("cancelled mid-flight" in m and _names_every_fact(m) for m in msgs), (
        f"a cancelled rating was never logged with its context: {msgs}"
    )


def _no_submit(monkeypatch) -> None:
    """Make the ledger explode if anything reaches it."""

    async def _exploding_submit(*args, **kwargs) -> dict:
        raise AssertionError("a rating that cannot be formed must never reach the stellar client")

    monkeypatch.setattr(sc, "submit_rating_async", _exploding_submit)


def test_a_step_the_settlement_lacks_raises_instead_of_rating(monkeypatch, caplog) -> None:
    """The refund was paid against this step, so its absence is a record that
    changed under a paid dispute. An outcome would file it as a rating to try
    again; only a raise stops the caller treating it as routine."""
    _no_submit(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        with pytest.raises(LookupError):
            _rate(_dispute(step_index=7))

    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(DISPUTE_ID in m and JOB in m and "no step 7" in m and PAYER in m for m in msgs), (
        f"the unratable dispute was not logged with its context: {msgs}"
    )


@pytest.mark.parametrize("job_id_hex", ["9f8e7d6c", "not hex at all"])
def test_a_job_id_that_will_not_derive_raises_instead_of_rating(monkeypatch, caplog, job_id_hex: str) -> None:
    _no_submit(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="app.services.dispute_rating"):
        with pytest.raises(ValueError):
            _rate(_dispute(job_id_hex=job_id_hex))

    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(DISPUTE_ID in m and job_id_hex in m and PAYER in m for m in msgs), (
        f"the underivable dispute was not logged with its context: {msgs}"
    )


def test_no_dispute_rating_line_carries_the_signing_key(monkeypatch, caplog) -> None:
    """The scorer's key signs every rating, so every branch that logs is walked
    with a key configured — message and rendered traceback alike."""
    monkeypatch.setattr(settings, "stellar_signing_key", SIGNING_SECRET)
    formatter = logging.Formatter("%(message)s")
    answers: list[dict | BaseException] = [
        {"status": "SUCCESS", "hash": "rating_tx"},
        {"status": "FAILED", "hash": "failed_tx"},
        {"status": "timeout", "hash": "inflight_tx"},
        _refused(7),
        _refused(1),
        _refused(100),
        RuntimeError("soroban rpc unreachable"),
    ]

    with caplog.at_level(logging.DEBUG, logger="app.services.dispute_rating"):
        for answer in answers:
            _fake_submit(monkeypatch, answer)
            _rate()
        _fake_submit(monkeypatch, asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            _rate()
        with pytest.raises(LookupError):
            _rate(_dispute(step_index=7))
        with pytest.raises(ValueError):
            _rate(_dispute(job_id_hex="9f8e7d6c"))

    records = [r for r in caplog.records if r.name == "app.services.dispute_rating"]
    assert len(records) == len(answers) + 3
    for rec in records:
        assert SIGNING_SECRET not in formatter.format(rec)
