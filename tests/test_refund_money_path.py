"""The money path under failure: a credit that was never sent, a store that
goes away mid-uphold, a transfer lost after it was signed (Epic 4 hardening).

`tests/test_adjudication.py` pins the ORDER of `uphold` with the settler stubbed
at `execute_refund`. Most of what is asserted here needs the stub one layer
lower — at `sc.invoke_with_server_key_async` — because the question is what the
REAL `execute_refund` and `credit_refund` make of what the client raises, and a
stub above them would answer it for them.

Hermetic, like the rest of the suite: the in-memory dispute store and fake
chain answers. Nothing is signed, paid or submitted.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from types import SimpleNamespace
from typing import Any

import pytest
from stellar_sdk import Keypair
from test_adjudication import (  # noqa: F401 - autouse fixtures and helpers
    JOB,
    LANDED,
    STEPS,
    SVC_LOGGER,
    TASK,
    _fresh_state,
    _sign,
    a_dispute,
    invalidated,
    rater,
)
from test_refund_execution import _dispute as _upheld_dispute

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep
from app.services.dispute_svc import DisputeError, dispute_message
from app.services.execution_svc import _settled_usdc


def _chain(monkeypatch, *answers: dict[str, Any] | BaseException) -> list[tuple[str, str, list[Any]]]:
    """Stand in for the stellar client BELOW `execute_refund`.

    Each call pops the next answer: a dict is returned, an exception raised.
    Once the script runs out every call lands, so "the retry pays" can be
    asserted without scripting it.
    """
    calls: list[tuple[str, str, list[Any]]] = []
    script = list(answers)

    async def _invoke(contract_id: str, fn: str, args: list[Any]) -> dict[str, Any]:
        calls.append((contract_id, fn, args))
        answer = script.pop(0) if script else LANDED
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(sc, "signer_public_key", lambda: "GSETTLER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(asset_sac="CSAC_ASSET", payment_escrow="CESCROW"))
    monkeypatch.setattr(sc, "addr", lambda a: ("addr", a))
    monkeypatch.setattr(sc, "i128", lambda v: ("i128", v))
    monkeypatch.setattr(sc, "invoke_with_server_key_async", _invoke)
    return calls


def _stored(dispute_id: str) -> tuple[dispute_store.DisputeRecord, list[str]]:
    store = dispute_store.get_dispute_store()
    current = asyncio.run(store.get_dispute(dispute_id))
    assert current is not None
    return current, [c.dispute_id for c in asyncio.run(store.list_refund_claims())]


# ── B1: a refusal before anything was sent is FAILED, not TIMEOUT ──


@pytest.mark.parametrize(
    "why",
    [
        # SAC: the settler holds too little USDC, and simulation refuses it.
        sc.ContractError("prepare failed: HostError: Error(Contract, #10)", 10),
        sc.NotSubmittedError("load_account failed: rpc unreachable"),
        sc.NotSubmittedError("sign failed: no secret"),
        # The RPC answered the send with ERROR / TRY_AGAIN_LATER and holds nothing.
        sc.NotSubmittedError("submit failed: AAAA"),
    ],
    ids=["simulation", "load_account", "sign", "send_refused"],
)
def test_a_transfer_refused_before_the_send_hands_the_claim_back(monkeypatch, why) -> None:
    calls = _chain(monkeypatch, why)
    dispute = a_dispute()

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert (refused.value.code, refused.value.status_code) == ("refund_failed", 502)
    assert len(calls) == 1
    current, queue = _stored(dispute.id)
    assert (current.status, current.refund_tx, queue) == ("upheld", None, [])
    # The buyer can still be paid: the next uphold claims it and the transfer lands.
    assert asyncio.run(dispute_svc.uphold(dispute.id)).status == "credited"
    assert len(calls) == 2


def test_a_signing_key_that_will_not_parse_is_failed_and_releases(monkeypatch) -> None:
    """`config_gap` asks presence only, so a key that is set but is not a key
    passes it and fails inside `execute_refund` — before anything is sent."""
    sent: list[Any] = []

    async def _never(*args: Any) -> dict[str, Any]:  # pragma: no cover - must never run
        sent.append(args)
        return LANDED

    monkeypatch.setattr(sc, "invoke_with_server_key_async", _never)
    dispute = a_dispute()
    monkeypatch.setattr(settings, "stellar_signing_key", "SNOT-A-REAL-SECRET")
    sc._signer_keypair.cache_clear()
    try:
        assert refund_svc.config_gap() is None
        with pytest.raises(DisputeError) as refused:
            asyncio.run(dispute_svc.uphold(dispute.id))
    finally:
        sc._signer_keypair.cache_clear()

    assert refused.value.code == "refund_failed"
    assert sent == []
    current, queue = _stored(dispute.id)
    assert (current.status, queue) == ("upheld", [])


def test_not_submitted_is_the_only_raise_read_as_failed(monkeypatch) -> None:
    _chain(monkeypatch, sc.NotSubmittedError("prepare failed: x"), RuntimeError("the socket dropped"))

    first = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))
    second = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))

    assert (first.status, first.tx_hash) == ("FAILED", None)
    assert (second.status, second.tx_hash) == ("TIMEOUT", None)


# ── B4: a failure after the send keeps the hash of what was signed ──


def test_a_transfer_lost_after_the_send_records_its_signed_hash(monkeypatch) -> None:
    signed = "5e" * 32
    calls = _chain(monkeypatch, sc.InFlightError("send failed: connection reset", signed))
    dispute = a_dispute()

    with pytest.raises(DisputeError) as unconfirmed:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert unconfirmed.value.code == "refund_unconfirmed"
    current, queue = _stored(dispute.id)
    # Still held, never released — and now the record names the transaction.
    assert (current.status, current.refund_tx, queue) == ("crediting", signed, [dispute.id])
    assert len(calls) == 1


def test_a_raise_that_is_not_in_flight_still_has_no_hash(monkeypatch) -> None:
    """Only the client's own type vouches for a hash; anything else has none."""
    _chain(monkeypatch, sc.InFlightError("poll failed: x", "7a" * 32), ConnectionError("dropped"))

    first = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))
    second = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))

    assert (first.status, first.tx_hash) == ("TIMEOUT", "7a" * 32)
    assert (second.status, second.tx_hash) == ("TIMEOUT", None)


# ── B3: the store failing mid-uphold never strands a dispute nothing paid ──


class _Flaky:
    """The real store, with `method` failing once the claim has been taken."""

    def __init__(self, inner: Any, method: str, error: BaseException) -> None:
        self._inner, self._method, self._error, self._armed = inner, method, error, False

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name == "claim_refund":

            async def _claim(*args: Any, **kwargs: Any) -> Any:
                claimed = await attr(*args, **kwargs)
                self._armed = True
                return claimed

            return _claim
        if name == self._method:

            async def _maybe_fail(*args: Any, **kwargs: Any) -> Any:
                if self._armed:
                    raise self._error
                return await attr(*args, **kwargs)

            return _maybe_fail
        return attr


def _flaky(monkeypatch, method: str, error: BaseException) -> Any:
    real = dispute_store.get_dispute_store()
    monkeypatch.setattr(dispute_store, "_store", _Flaky(real, method, error))
    return real


@pytest.mark.parametrize(
    "error",
    [ConnectionError("database unreachable"), asyncio.CancelledError()],
    ids=["store_down", "cancelled"],
)
def test_a_failure_between_the_claim_and_the_transfer_hands_the_claim_back(monkeypatch, error) -> None:
    calls = _chain(monkeypatch)
    dispute = a_dispute()
    real = _flaky(monkeypatch, "get_settlement", error)

    with pytest.raises(type(error)):
        asyncio.run(dispute_svc.uphold(dispute.id))

    monkeypatch.setattr(dispute_store, "_store", real)
    assert calls == []  # nothing was signed
    current, queue = _stored(dispute.id)
    assert (current.status, current.refund_tx, queue) == ("upheld", None, [])
    assert asyncio.run(dispute_svc.uphold(dispute.id)).status == "credited"


def test_a_bug_while_pricing_the_credit_hands_the_claim_back(monkeypatch) -> None:
    calls = _chain(monkeypatch)
    dispute = a_dispute()

    def _broken(*args: Any, **kwargs: Any) -> float:
        raise TypeError("a bug in the arithmetic")

    monkeypatch.setattr(refund_svc, "creditable_for", _broken)
    with pytest.raises(TypeError):
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert calls == []
    current, queue = _stored(dispute.id)
    assert (current.status, queue) == ("upheld", [])


def test_a_release_that_fails_after_failed_records_the_failed_hash(monkeypatch) -> None:
    calls = _chain(monkeypatch, {"status": "FAILED", "hash": "tx_rejected"})
    dispute = a_dispute()
    real = _flaky(monkeypatch, "release_refund_claim", ConnectionError("database unreachable"))

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    monkeypatch.setattr(dispute_store, "_store", real)
    assert refused.value.code == "refund_failed"
    assert "released by hand" in str(refused.value)
    assert len(calls) == 1
    current, queue = _stored(dispute.id)
    # Held (the release failed), but no longer indistinguishable from a
    # transfer in flight: the record names the one the ledger rejected.
    assert (current.status, current.refund_tx, queue) == ("crediting", "tx_rejected", [dispute.id])


def test_a_release_that_fails_on_a_refusal_still_answers_the_refusal(monkeypatch) -> None:
    calls = _chain(monkeypatch)
    dispute = a_dispute()
    monkeypatch.setattr(settings, "max_refund_usdc", 0.01)
    _flaky(monkeypatch, "release_refund_claim", ConnectionError("database unreachable"))

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refund_above_cap"
    assert calls == []


def test_a_cancel_after_the_transfer_was_handed_over_holds_the_claim(monkeypatch) -> None:
    """The guard ends where the transfer begins: a cancellation there may have
    come after the send, so the claim is kept whatever else happens."""
    calls = _chain(monkeypatch, asyncio.CancelledError())
    dispute = a_dispute()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert len(calls) == 1
    current, queue = _stored(dispute.id)
    assert (current.status, queue) == ("crediting", [dispute.id])


# ── B6: the writes after a transfer are compare-and-set on `crediting` ──


def _operator_releases_mid_flight(monkeypatch, answer: dict[str, Any]) -> list[Any]:
    """The transfer is out when somebody hands the claim back by hand."""
    calls: list[Any] = []

    async def _invoke(contract_id: str, fn: str, args: list[Any]) -> dict[str, Any]:
        calls.append(fn)
        store = dispute_store.get_dispute_store()
        [claim] = await store.list_refund_claims()
        await store.release_refund_claim(claim.dispute_id)
        return answer

    _chain(monkeypatch)
    monkeypatch.setattr(sc, "invoke_with_server_key_async", _invoke)
    return calls


def test_a_credit_landing_on_a_dispute_moved_mid_flight_is_recorded_and_critical(monkeypatch, caplog) -> None:
    calls = _operator_releases_mid_flight(monkeypatch, {"status": "SUCCESS", "hash": "tx_landed"})
    dispute = a_dispute()

    with caplog.at_level(logging.CRITICAL, logger=SVC_LOGGER):
        credited = asyncio.run(dispute_svc.uphold(dispute.id))

    assert calls == ["transfer"]
    # The money moved, so the record says so whatever happened to it meanwhile.
    assert (credited.status, credited.refund_tx) == ("credited", "tx_landed")
    critical = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and r.levelno == logging.CRITICAL]
    assert any("tx_landed" in m and "no longer in crediting" in m and "now upheld" in m for m in critical), critical


def test_an_unconfirmed_credit_never_overwrites_a_dispute_moved_mid_flight(monkeypatch, caplog) -> None:
    _operator_releases_mid_flight(monkeypatch, {"status": "timeout", "hash": "tx_inflight"})
    dispute = a_dispute()

    with caplog.at_level(logging.CRITICAL, logger=SVC_LOGGER):
        with pytest.raises(DisputeError) as unconfirmed:
            asyncio.run(dispute_svc.uphold(dispute.id))

    assert unconfirmed.value.code == "refund_unconfirmed"
    current, _ = _stored(dispute.id)
    assert (current.status, current.refund_tx) == ("upheld", None)
    critical = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and r.levelno == logging.CRITICAL]
    assert any("tx_inflight" in m and "MAY STILL LAND" in m and "now upheld" in m for m in critical), critical


# ── B5: the settled total bounds a workflow's credits together ──


def _two_disputes_of_one_job(settled_usdc: float) -> tuple[DisputeRecord, DisputeRecord]:
    """One buyer, one settlement, both steps disputed — opened as a buyer opens them."""
    payer = Keypair.random()
    now = time.time()
    asyncio.run(
        dispute_store.get_dispute_store().record_settlement(
            SettlementRecord(
                TASK, payer.public_key, "ab" * 16, JOB, "tx_c", "tx_p", settled_usdc, STEPS, now, now + 3600
            )
        )
    )
    opened = []
    for step in (0, 1):
        nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, step))
        opened.append(
            asyncio.run(
                dispute_svc.open_dispute(
                    job_id_hex=JOB,
                    step_index=step,
                    reason="it did not deliver",
                    payer=payer.public_key,
                    nonce=nonce,
                    signature_b64=_sign(payer, dispute_message(JOB, step, nonce)),
                )
            )
        )
    return opened[0], opened[1]


def test_a_second_credit_is_bounded_by_what_the_first_left_of_the_settlement(monkeypatch) -> None:
    """Steps of 0.05 and 0.07 whose charge settled 0.10 in all: each step alone
    fits under the settled total, both together do not."""
    calls = _chain(monkeypatch)
    first, second = _two_disputes_of_one_job(settled_usdc=0.10)

    assert asyncio.run(dispute_svc.uphold(second.id)).credited_usdc == 0.07
    assert asyncio.run(dispute_svc.uphold(first.id)).credited_usdc == pytest.approx(0.03, abs=1e-12)
    assert [args[2][1] for _, _, args in calls] == [700_000, 300_000]


def test_a_credit_still_in_flight_is_reserved_at_its_promise(monkeypatch) -> None:
    calls = _chain(monkeypatch, {"status": "timeout", "hash": "tx_inflight"})
    first, second = _two_disputes_of_one_job(settled_usdc=0.10)

    with pytest.raises(DisputeError):
        asyncio.run(dispute_svc.uphold(second.id))  # 0.07 out, may still land
    assert asyncio.run(dispute_svc.uphold(first.id)).credited_usdc == pytest.approx(0.03, abs=1e-12)
    assert [args[2][1] for _, _, args in calls] == [700_000, 300_000]


def test_nothing_left_of_the_settlement_is_nothing_to_credit(monkeypatch) -> None:
    calls = _chain(monkeypatch)
    first, second = _two_disputes_of_one_job(settled_usdc=0.07)

    asyncio.run(dispute_svc.uphold(second.id))
    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(first.id))

    assert refused.value.code == "nothing_to_credit"
    assert len(calls) == 1
    current, queue = _stored(first.id)
    assert (current.status, queue) == ("upheld", [])


def _credit_every_step(prices: list[float]) -> tuple[int, int]:
    """(stroops charged, stroops credited) when every step of a job is credited in turn."""
    steps = tuple(SettlementStep(i, f"agt_{i}", None, p, True) for i, p in enumerate(prices))
    spent = 0.0
    for p in prices:
        spent += p
    settlement = SettlementRecord(TASK, "GP", "ab" * 16, JOB, "tx", None, _settled_usdc(spent), steps, 0.0, 1.0)
    paid: list[float] = []
    for i, p in enumerate(prices):
        promise = refund_svc.credited_amount_usdc(p)
        dispute = DisputeRecord(f"dsp_{i}", JOB, TASK, i, f"agt_{i}", "GP", "r", "crediting", p, promise, 0.0)
        try:
            paid.append(refund_svc.creditable_for(settlement, dispute, credited_elsewhere_usdc=tuple(paid)))
        except refund_svc.RefundRefused:
            continue
    return sc.usdc_to_i128(max(spent, 0.000001)), sum(sc.usdc_to_i128(c) for c in paid)


def test_the_audits_worst_case_is_no_longer_over_credited(monkeypatch) -> None:
    monkeypatch.setattr(refund_svc.logger, "disabled", True)
    charged, credited = _credit_every_step([8e-8, 8e-8, 0.01614268, 8e-8, 7e-8, 0.195193159])
    assert charged == 2_113_361
    assert credited == charged  # was 2_113_363: two stroops the charge never moved


def test_no_job_is_ever_credited_a_stroop_more_than_it_was_charged(monkeypatch) -> None:
    """The audit's brute force, cut to 20,000 jobs: it found over-credits in
    about one job in fifteen, of up to 2 stroops, before the net bound."""
    monkeypatch.setattr(refund_svc.logger, "disabled", True)
    rng = random.Random(7)
    over = []
    for _ in range(20_000):
        prices = [
            rng.choice([rng.randint(1, 9) * 10**-8, round(rng.uniform(0, 0.2), rng.randint(3, 9))])
            for _ in range(rng.randint(1, 6))
        ]
        charged, credited = _credit_every_step(prices)
        if credited > charged:
            over.append((prices, charged, credited))
    assert over == []
