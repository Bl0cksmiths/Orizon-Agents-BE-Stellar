"""`dispute_svc.uphold`, pinned as the exact sequence of store calls and seams it makes on every path.

`tests/test_adjudication.py` proves what each path MEANS for the buyer; this
file pins what each path DOES, call by call, so `uphold` can be split into
helpers without one store write, release, transfer or log line moving: the
master switch, the three already-adjudicated answers, the configuration gate,
the `open → upheld` compare-and-set and its lost race, the claim, the amount,
the transfer's three outcomes, and every way recording them can fail.

The store is the in-memory one, wrapped to record each call and to answer a
scripted value or raise where a path needs it. The transfer, the amount, the
workflow note and the rating are recorders at the service's own seams. No
chain, no network, no database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import pytest
from test_dispute_store import a_dispute, a_settlement

from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
from app.services.dispute_store import DisputeRecord, InMemoryDisputeStore, SettlementRecord
from app.services.dispute_svc import DisputeError

LOGGER = "app.services.dispute_svc"
DISPUTE = "dsp_0001"
REAL = object()  # a scripted answer that lets the real store answer


class Store(InMemoryDisputeStore):
    """The in-memory store, recording every call `uphold` makes and answering from `script` first."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, ...]] = []
        self.script: dict[str, list[Any]] = {}
        self.recording = False

    async def _answer(self, name: str, real: Any, *args: Any, **kwargs: Any) -> Any:
        if self.recording:
            self.calls.append((name, *args, *(f"{k}={v}" for k, v in sorted(kwargs.items()))))
        queued = self.script.get(name)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            if answer is not REAL:
                return answer
        return await real(*args, **kwargs)

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        return await self._answer("get_dispute", super().get_dispute, dispute_id)

    async def append_status(self, dispute_id: str, status: Any, **kwargs: Any) -> DisputeRecord | None:
        return await self._answer("append_status", super().append_status, dispute_id, status, **kwargs)

    async def claim_refund(self, dispute_id: str) -> DisputeRecord | None:
        return await self._answer("claim_refund", super().claim_refund, dispute_id)

    async def get_settlement(self, job_id_hex: str) -> SettlementRecord | None:
        return await self._answer("get_settlement", super().get_settlement, job_id_hex)

    async def list_disputes_for_task(self, task_id: str) -> tuple[DisputeRecord, ...]:
        return await self._answer("list_disputes_for_task", super().list_disputes_for_task, task_id)

    async def release_refund_claim(self, dispute_id: str, **kwargs: Any) -> DisputeRecord | None:
        return await self._answer("release_refund_claim", super().release_refund_claim, dispute_id, **kwargs)


class Seams:
    """The service seams `uphold` reaches beyond the store, each recording what it was handed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.gap: refund_svc.ConfigGap | None = None
        self.amount: float | BaseException = 0.05
        self.outcome: refund_svc.RefundOutcome | BaseException = refund_svc.RefundOutcome("SUCCESS", "tx_credit", 0.05)
        monkeypatch.setattr(refund_svc, "config_gap", lambda: self.gap)
        monkeypatch.setattr(refund_svc, "creditable_for", self._creditable_for)
        monkeypatch.setattr(refund_svc, "credit_refund", self._credit_refund)
        monkeypatch.setattr(dispute_svc, "_note_credit_on_workflow", self._note)
        monkeypatch.setattr(dispute_svc, "_rate_credited", self._rate)
        monkeypatch.setattr(dispute_svc, "_retry_rating", self._retry)

    def _creditable_for(
        self, settlement: SettlementRecord, dispute: DisputeRecord, fraction: float, *, credited_elsewhere_usdc: Any
    ) -> float:
        self.calls.append(("creditable_for", settlement.job_id_hex, dispute.id, fraction, credited_elsewhere_usdc))
        if isinstance(self.amount, BaseException):
            raise self.amount
        return self.amount

    async def _credit_refund(self, dispute: DisputeRecord, amount_usdc: float) -> refund_svc.RefundOutcome:
        self.calls.append(("credit_refund", dispute.id, dispute.status, amount_usdc))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def _note(self, dispute: DisputeRecord, amount_usdc: float, tx_hash: str | None) -> None:
        self.calls.append(("note_credit", dispute.id, dispute.status, amount_usdc, tx_hash))

    async def _rate(self, credited: DisputeRecord, settlement: SettlementRecord, *, on_rating: Any) -> DisputeRecord:
        self.calls.append(("rate_credited", credited.id, credited.status, settlement.job_id_hex, on_rating))
        return dataclasses.replace(credited, rating_tx="tx_rated")

    async def _retry(self, credited: DisputeRecord, *, on_rating: Any) -> DisputeRecord:
        self.calls.append(("retry_rating", credited.id, credited.status, on_rating))
        return dataclasses.replace(credited, rating_tx="tx_retried")


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "dispute_credited_fraction", 1.0)
    chosen = Store()
    dispute_store._store = chosen
    yield chosen
    dispute_store._store = None


@pytest.fixture()
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    return Seams(monkeypatch)


def _seed(store: Store, status: str = "open", **fields: Any) -> DisputeRecord:
    async def go() -> DisputeRecord:
        await store.record_settlement(a_settlement())
        opened = await store.open_dispute(a_dispute())
        if status in ("upheld", "crediting", "credited", "rejected"):
            await store.append_status(opened.id, "upheld")
        if status in ("crediting", "credited"):
            assert await store.claim_refund(opened.id) is not None
        if status in ("credited", "rejected"):
            await store.append_status(opened.id, status, **fields)
        elif fields:
            await store.append_status(opened.id, status, **fields)
        current = await store.get_dispute(opened.id)
        assert current is not None
        assert current.status == status
        return current

    seeded = asyncio.run(go())
    store.recording = True
    return seeded


def _uphold(on_rating: Any = None) -> DisputeRecord:
    return asyncio.run(dispute_svc.uphold(DISPUTE, on_rating=on_rating))


def _refused(on_rating: Any = None) -> DisputeError:
    with pytest.raises(DisputeError) as exc:
        _uphold(on_rating)
    return exc.value


def _lines(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == level]


def _status(store: Store) -> str:
    current = asyncio.run(InMemoryDisputeStore.get_dispute(store, DISPUTE))
    assert current is not None
    return current.status


CAS_UPHELD = ("append_status", DISPUTE, "upheld", "expected_status=open")
CLAIM = ("claim_refund", DISPUTE)
LOAD = ("get_dispute", DISPUTE)
SETTLEMENT = ("get_settlement", "ab" * 32)
SIBLINGS = ("list_disputes_for_task", "task_alpha")
RELEASE = ("release_refund_claim", DISPUTE)
CREDITED = (
    "append_status",
    DISPUTE,
    "credited",
    "credited_usdc=0.05",
    "expected_status=crediting",
    "refund_tx=tx_credit",
)
IN_FLIGHT = (
    "append_status",
    DISPUTE,
    "crediting",
    "expected_status=crediting",
    "inflight_usdc=0.05",
    "refund_tx=tx_lost",
)
TIMEOUT = refund_svc.RefundOutcome("TIMEOUT", "tx_lost", 0.05)
FAILED = refund_svc.RefundOutcome("FAILED", "tx_failed", 0.05)


# ── before anything is claimed ──────────────────────────────────────────────


def test_the_master_switch_refuses_before_the_store_is_read(
    store: Store, seams: Seams, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(store)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        err = _refused()
    assert (err.code, err.status_code) == ("refunds_disabled", 503)
    assert store.calls == []
    assert seams.calls == []
    assert _lines(caplog, logging.WARNING) == [f"adjudication refused: dispute={DISPUTE} reason=refunds_disabled"]


def test_an_unknown_dispute_is_a_404(store: Store, seams: Seams) -> None:
    store.recording = True
    err = _refused()
    assert (err.code, err.status_code) == ("unknown_dispute", 404)
    assert store.calls == [LOAD]
    assert seams.calls == []


def test_a_credited_dispute_retries_its_rating_and_signs_nothing(store: Store, seams: Seams) -> None:
    _seed(store, "credited", refund_tx="tx_old", credited_usdc=0.05)
    observer = object()
    result = _uphold(observer)
    assert result.rating_tx == "tx_retried"
    assert store.calls == [LOAD]
    assert seams.calls == [("retry_rating", DISPUTE, "credited", observer)]


@pytest.mark.parametrize(("status", "code"), [("crediting", "refund_in_flight"), ("rejected", "dispute_rejected")])
def test_an_answered_dispute_is_refused_before_anything_else(
    store: Store, seams: Seams, status: str, code: str
) -> None:
    _seed(store, status)
    err = _refused()
    assert (err.code, err.status_code) == (code, 409)
    assert store.calls == [LOAD]
    assert seams.calls == []


def test_a_deployment_that_cannot_sign_claims_nothing(
    store: Store, seams: Seams, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(store)
    seams.gap = refund_svc.ConfigGap("STELLAR_ASSET_SAC is unset", "there is no asset contract to pay a credit over")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        err = _refused()
    assert (err.code, err.status_code) == ("refunds_not_configured", 503)
    assert err.message == "this deployment cannot sign a credit: there is no asset contract to pay a credit over"
    assert store.calls == [LOAD] and seams.calls == []
    errors = _lines(caplog, logging.ERROR)
    assert errors[0] == (
        f"dispute {DISPUTE} cannot be credited — STELLAR_ASSET_SAC is unset; nothing was claimed and nothing was"
        f" signed (job {'ab' * 32}, payer {a_dispute().payer})"
    )
    assert errors[1].endswith("amount=1.5000000 tx=- reason=refunds_not_configured")
    assert _status(store) == "open"


# ── the open → upheld compare-and-set ───────────────────────────────────────


def test_a_lost_race_to_a_credit_answers_with_the_credit(store: Store, seams: Seams) -> None:
    _seed(store)
    credited = dataclasses.replace(a_dispute(), status="credited", refund_tx="tx_theirs")
    store.script = {"append_status": [None], "get_dispute": [REAL, credited]}
    assert _uphold().rating_tx == "tx_retried"
    assert store.calls == [LOAD, CAS_UPHELD, LOAD]
    assert seams.calls == [("retry_rating", DISPUTE, "credited", None)]


def test_a_lost_race_to_another_adjudication_refuses(store: Store, seams: Seams) -> None:
    _seed(store)
    upheld = dataclasses.replace(a_dispute(), status="upheld", refund_tx="tx_x")
    store.script = {"append_status": [None], "get_dispute": [REAL, upheld]}
    err = _refused()
    assert (err.code, err.status_code) == ("adjudication_in_progress", 409)
    assert store.calls == [LOAD, CAS_UPHELD, LOAD] and seams.calls == []


@pytest.mark.parametrize(("raced_to", "code"), [("crediting", "refund_in_flight"), ("rejected", "dispute_rejected")])
def test_a_lost_race_to_an_answer_is_that_answer(store: Store, seams: Seams, raced_to: str, code: str) -> None:
    _seed(store)
    store.script = {"append_status": [None], "get_dispute": [REAL, dataclasses.replace(a_dispute(), status=raced_to)]}
    assert _refused().code == code


# ── the claim and the amount ────────────────────────────────────────────────


@pytest.mark.parametrize("reread", [REAL, None])
def test_a_claim_somebody_else_holds_returns_the_dispute_unsigned(
    store: Store, seams: Seams, reread: Any, caplog: pytest.LogCaptureFixture
) -> None:
    seeded = _seed(store, "upheld")
    store.script = {"claim_refund": [None], "get_dispute": [REAL, reread]}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = _uphold()
    assert result == seeded
    assert store.calls == [LOAD, CLAIM, LOAD] and seams.calls == []
    assert _lines(caplog, logging.INFO)[-1] == f"dispute {DISPUTE} is already claimed (upheld) — nothing signed here"


def test_an_upheld_dispute_goes_straight_to_the_claim_and_is_credited(store: Store, seams: Seams) -> None:
    _seed(store, "upheld")
    observer = object()
    result = _uphold(observer)
    assert result.rating_tx == "tx_rated" and result.status == "credited"
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, CREDITED]
    assert seams.calls == [
        ("creditable_for", "ab" * 32, DISPUTE, 1.0, ()),
        ("credit_refund", DISPUTE, "crediting", 0.05),
        ("note_credit", DISPUTE, "credited", 0.05, "tx_credit"),
        ("rate_credited", DISPUTE, "credited", "ab" * 32, observer),
    ]


def test_an_open_dispute_is_upheld_then_claimed_then_credited(store: Store, seams: Seams) -> None:
    _seed(store)
    _uphold()
    assert store.calls == [LOAD, CAS_UPHELD, CLAIM, SETTLEMENT, SIBLINGS, CREDITED]
    assert [c[0] for c in seams.calls] == ["creditable_for", "credit_refund", "note_credit", "rate_credited"]


def test_a_refused_amount_hands_the_claim_back(store: Store, seams: Seams) -> None:
    _seed(store, "upheld")
    seams.amount = refund_svc.RefundRefused("refund_above_cap", "0.05 is above the cap")
    err = _refused()
    assert (err.code, err.status_code, err.message) == ("refund_above_cap", 409, "0.05 is above the cap")
    assert err.__cause__ is None and err.__suppress_context__ is True
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, RELEASE]
    assert [c[0] for c in seams.calls] == ["creditable_for"]
    assert _status(store) == "upheld"


def test_a_failure_before_anything_is_signed_hands_the_claim_back_and_raises_it(store: Store, seams: Seams) -> None:
    _seed(store, "upheld")
    store.script = {"get_settlement": [ConnectionError("store down")]}
    with pytest.raises(ConnectionError, match="store down"):
        _uphold()
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, RELEASE] and seams.calls == []


def test_a_cancellation_before_anything_is_signed_hands_the_claim_back(store: Store, seams: Seams) -> None:
    _seed(store, "upheld")
    store.script = {"list_disputes_for_task": [asyncio.CancelledError()]}
    with pytest.raises(asyncio.CancelledError):
        _uphold()
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, RELEASE]


def test_a_missing_settlement_reads_no_siblings_and_hands_the_claim_back(
    store: Store, seams: Seams, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(store, "upheld")
    store.script = {"get_settlement": [None]}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        err = _refused()
    assert (err.code, err.status_code) == ("settlement_missing", 409)
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, RELEASE] and seams.calls == []
    assert _lines(caplog, logging.ERROR)[-1].endswith("amount=1.5000000 tx=- reason=settlement_missing")


def test_a_transfer_refused_before_signing_hands_the_claim_back(store: Store, seams: Seams) -> None:
    _seed(store, "upheld")
    seams.outcome = refund_svc.RefundRefused("refund_amount_invalid", "nan is not an amount")
    err = _refused()
    assert (err.code, err.status_code, err.message) == ("refund_amount_invalid", 409, "nan is not an amount")
    assert err.__suppress_context__ is True
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, RELEASE]
    assert [c[0] for c in seams.calls] == ["creditable_for", "credit_refund"]
    assert _status(store) == "upheld"


# ── SUCCESS ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("moved", "described"),
    [
        (dataclasses.replace(a_dispute(), status="upheld", refund_tx="tx_other"), "now upheld, refund tx tx_other"),
        (None, "now missing, refund tx -"),
    ],
)
def test_a_landed_credit_on_a_dispute_moved_mid_flight_is_recorded_anyway(
    store: Store, seams: Seams, caplog: pytest.LogCaptureFixture, moved: Any, described: str
) -> None:
    _seed(store, "upheld")
    store.script = {"append_status": [None], "get_dispute": [REAL, moved]}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = _uphold()
    assert result.status == "credited" and result.refund_tx == "tx_credit"
    unconditional = ("append_status", DISPUTE, "credited", "credited_usdc=0.05", "refund_tx=tx_credit")
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, CREDITED, LOAD, unconditional]
    (critical,) = _lines(caplog, logging.CRITICAL)
    assert critical.startswith(f"dispute {DISPUTE}: 0.0500000 USDC LANDED as tx tx_credit on a dispute no longer")
    assert f"({described}) — an operator intervened mid-flight" in critical
    assert [c[0] for c in seams.calls] == ["creditable_for", "credit_refund", "note_credit", "rate_credited"]


@pytest.mark.parametrize(
    "script",
    [
        {"append_status": [ConnectionError("store down")]},
        {"append_status": [None, None], "get_dispute": [REAL, REAL]},
    ],
    ids=["write_raises", "unconditional_write_refused"],
)
def test_a_landed_credit_that_cannot_be_recorded_is_logged_and_raised(
    store: Store, seams: Seams, caplog: pytest.LogCaptureFixture, script: dict[str, list[Any]]
) -> None:
    _seed(store, "upheld")
    store.script = script
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises((ConnectionError, RuntimeError)):
        _uphold()
    assert _lines(caplog, logging.ERROR)[-1] == (
        f"dispute {DISPUTE}: 0.0500000 USDC LANDED as tx tx_credit and the credit could NOT be recorded — the"
        f" dispute stays crediting with its claim held; record it by hand (job {'ab' * 32}, payer"
        f" {a_dispute().payer})"
    )
    assert [c[0] for c in seams.calls] == ["creditable_for", "credit_refund"]
    assert _status(store) == "crediting"


# ── FAILED ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("release", "message", "status"),
    [
        (
            REAL,
            "the credit transfer did not settle, so nothing was paid — the dispute is still upheld and can be"
            " credited again",
            "upheld",
        ),
        (
            ConnectionError("store down"),
            "the credit transfer did not settle, so nothing was paid — but the dispute could not be handed back,"
            " and it stays crediting until it is released by hand",
            "crediting",
        ),
    ],
    ids=["released", "not_released"],
)
def test_a_failed_transfer_is_a_502_and_hands_the_claim_back(
    store: Store, seams: Seams, release: Any, message: str, status: str
) -> None:
    _seed(store, "upheld")
    seams.outcome = FAILED
    store.script = {"release_refund_claim": [release]}
    err = _refused()
    assert (err.code, err.status_code, err.message) == ("refund_failed", 502, message)
    failed_on_record = ("append_status", DISPUTE, "crediting", "expected_status=crediting", "inflight_usdc=0.05")
    tail = [] if release is REAL else [(*failed_on_record, "refund_tx=tx_failed")]
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, RELEASE, *tail]
    assert _status(store) == status


# ── TIMEOUT ─────────────────────────────────────────────────────────────────


def test_an_unconfirmed_transfer_records_its_hash_keeps_the_claim_and_is_a_504(store: Store, seams: Seams) -> None:
    _seed(store, "upheld")
    seams.outcome = TIMEOUT
    err = _refused()
    assert (err.code, err.status_code) == ("refund_unconfirmed", 504)
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, IN_FLIGHT]
    current = asyncio.run(InMemoryDisputeStore.get_dispute(store, DISPUTE))
    assert current is not None and (current.status, current.refund_tx, current.inflight_usdc) == (
        "crediting",
        "tx_lost",
        0.05,
    )


@pytest.mark.parametrize(
    ("moved", "now"), [(dataclasses.replace(a_dispute(), status="upheld"), "upheld"), (None, "missing")]
)
def test_an_unconfirmed_transfer_on_a_moved_dispute_is_critical_and_still_a_504(
    store: Store, seams: Seams, caplog: pytest.LogCaptureFixture, moved: Any, now: str
) -> None:
    _seed(store, "upheld")
    seams.outcome = TIMEOUT
    store.script = {"append_status": [None], "get_dispute": [REAL, moved]}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        err = _refused()
    assert err.code == "refund_unconfirmed"
    assert store.calls == [LOAD, CLAIM, SETTLEMENT, SIBLINGS, IN_FLIGHT, LOAD]
    (critical,) = _lines(caplog, logging.CRITICAL)
    assert critical == (
        f"dispute {DISPUTE}: refund tx tx_lost is UNCONFIRMED and MAY STILL LAND, but the dispute is no longer in"
        f" crediting (now {now}) — an operator intervened mid-flight; it was NOT overwritten, and a second credit"
        f" may follow — reconcile by hand (job {'ab' * 32}, payer {a_dispute().payer}, 0.0500000 USDC)"
    )


def test_an_unconfirmed_hash_that_cannot_be_recorded_is_logged_and_still_a_504(
    store: Store, seams: Seams, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(store, "upheld")
    seams.outcome = TIMEOUT
    store.script = {"append_status": [ConnectionError("store down")]}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        err = _refused()
    assert (err.code, err.status_code) == ("refund_unconfirmed", 504)
    errors = _lines(caplog, logging.ERROR)
    assert errors[-2] == (
        f"dispute {DISPUTE}: the in-flight refund hash tx_lost could NOT be recorded — the dispute is crediting"
        f" with its claim held, and the hash is in this line only (job {'ab' * 32}, payer {a_dispute().payer},"
        " 0.0500000 USDC)"
    )
    assert errors[-1].endswith("amount=0.0500000 tx=tx_lost reason=refund_unconfirmed")
