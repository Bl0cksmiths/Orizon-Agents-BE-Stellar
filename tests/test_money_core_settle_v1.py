"""Characterization of `_settle_onchain` (the v1 charge + seal) at the edges the
logging suite does not reach: how the charge's receipt id is decoded into the
seal, what a confirmed charge and seal trace, and how a failure after the
charge, or an in-flight charge, is reported."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from stellar_sdk import scval
from test_settlement_logging import AUTH_ID_HEX, PAYER, _plan, _use_fake_signer

from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

TASK = "tsk_mc_v1"
CHARGE_TX = "c1" * 32
SEAL_TX = "5a" * 32
RECEIPT = bytes(range(16))


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    state.traces.pop(TASK, None)
    state.tasks.pop(TASK, None)


class _V1Chain:
    def __init__(self, *, charge: Any, seal: Any = None) -> None:
        self.charge = charge
        self.seal = seal
        self.calls: list[tuple[str, list[Any]]] = []

    async def invoke(self, contract_id: str, function_name: str, args: list[Any]) -> dict[str, Any]:
        self.calls.append((function_name, args))
        answer = self.charge if function_name == "charge" else self.seal
        if isinstance(answer, BaseException):
            raise answer
        return answer or {"hash": SEAL_TX, "status": "SUCCESS"}


def _install(monkeypatch: pytest.MonkeyPatch, chain: _V1Chain) -> _V1Chain:
    _use_fake_signer(monkeypatch)
    monkeypatch.setattr(sc, "invoke_with_server_key_async", chain.invoke)
    return chain


def _settle(**kwargs: Any) -> tuple[str | None, str | None, bytes | None]:
    return asyncio.run(
        execution_svc._settle_onchain(
            TASK, time.monotonic(), _plan(), payer=PAYER, auth_id_hex=AUTH_ID_HEX, total_usdc=0.05, **kwargs
        )
    )


def _lines() -> list[tuple[str, str, str | None]]:
    return [(line.level, line.msg, line.settlement) for line in state.traces.get(TASK, [])]


def _sealed_receipts(chain: _V1Chain) -> list[Any]:
    [seal] = [args for name, args in chain.calls if name == "seal"]
    return scval.to_native(seal[5])


@pytest.mark.parametrize("result", [RECEIPT, bytearray(RECEIPT), RECEIPT.hex()], ids=["bytes", "bytearray", "hex"])
def test_a_decodable_receipt_id_is_sealed(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    chain = _install(monkeypatch, _V1Chain(charge={"hash": CHARGE_TX, "status": "SUCCESS", "result": result}))

    charge_tx, proof_tx, job_id = _settle()

    assert (charge_tx, proof_tx) == (CHARGE_TX, SEAL_TX)
    assert job_id is not None
    assert _sealed_receipts(chain) == [RECEIPT]
    assert not any("receipt id missing" in msg for _, msg, _ in _lines())


@pytest.mark.parametrize(
    "result", ["not-hex", "ab" * 8, b"\x01" * 15, None, 7], ids=["bad-hex", "short-hex", "short-bytes", "none", "int"]
)
def test_an_undecodable_receipt_id_seals_without_a_link_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, result: Any
) -> None:
    chain = _install(monkeypatch, _V1Chain(charge={"hash": CHARGE_TX, "status": "SUCCESS", "result": result}))

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        _, proof_tx, job_id = _settle()

    assert proof_tx == SEAL_TX
    assert job_id is not None
    assert _sealed_receipts(chain) == []
    assert ("error", "charge receipt id missing — sealing attestation without receipt link", None) in _lines()
    assert [r.getMessage() for r in caplog.records] == [
        f"task {TASK}: charge result did not decode to a 16-byte receipt id — sealing without receipt link "
        f"(result={result!r}, job {job_id.hex()}, charge_tx {CHARGE_TX}, auth {AUTH_ID_HEX}, payer {PAYER})"
    ]


def test_a_confirmed_charge_and_seal_trace_in_order_and_hand_the_charge_on_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _install(monkeypatch, _V1Chain(charge={"hash": CHARGE_TX, "status": "SUCCESS", "result": RECEIPT.hex()}))
    seen: list[tuple[str, bytes, int]] = []

    async def on_charged(charge_tx: str, job_id: bytes) -> None:
        seen.append((charge_tx, job_id, len(chain.calls)))

    _, _, job_id = _settle(on_charged=on_charged)

    assert seen == [(CHARGE_TX, job_id, 1)]
    amount = execution_svc._amount(sc.usdc_to_i128(0.05))
    lines = _lines()
    assert lines[:2] == [
        ("cost", f"x402 charge → {amount} settled · tx {CHARGE_TX[:10]}…", "settled"),
        ("proof", f"ERC-8004 attestation sealed · tx {SEAL_TX[:10]}…", None),
    ]
    assert lines[2][1].startswith(f"workflow sealed — 1 agents · {amount} · ")
    assert len(lines) == 3


def test_a_failure_after_the_charge_confirmed_keeps_the_charge(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(
        monkeypatch,
        _V1Chain(charge={"hash": CHARGE_TX, "status": "SUCCESS", "result": RECEIPT.hex()}, seal=RuntimeError("down")),
    )

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        charge_tx, proof_tx, job_id = _settle()

    assert (charge_tx, proof_tx) == (CHARGE_TX, None) and job_id is not None
    assert _lines()[-1] == ("error", "on-chain settlement failed", None)
    [record] = caplog.records
    assert record.exc_info is not None
    assert record.getMessage() == (
        f"task {TASK}: on-chain settlement failed: down (job {job_id.hex()}, auth {AUTH_ID_HEX}, payer {PAYER}, "
        f"0.050000 USDC, charge_tx={CHARGE_TX}, proof_tx=None)"
    )


def test_a_charge_in_flight_is_reported_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _install(monkeypatch, _V1Chain(charge=sc.InFlightError("poll failed", CHARGE_TX)))

    assert _settle() == (None, None, None)

    assert [name for name, _ in chain.calls] == ["charge"]
    assert _lines() == [
        (
            "error",
            "on-chain settlement unconfirmed — it may still settle, and this run cannot be disputed",
            "unconfirmed",
        )
    ]


def test_a_charge_that_raised_otherwise_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _V1Chain(charge=sc.NotSubmittedError("refused")))

    assert _settle() == (None, None, None)

    assert _lines() == [("error", "on-chain settlement failed", "failed")]


@pytest.mark.parametrize(
    ("answer", "line", "trace", "settlement"),
    [
        (
            {"hash": CHARGE_TX, "status": "FAILED"},
            "task {task}: PaymentEscrow.charge did not settle — status=FAILED hash={tx} "
            "(auth {auth}, payer {payer}, 0.050000 USDC, job {job})",
            "charge status=FAILED hash={tx}",
            "failed",
        ),
        (
            {"hash": CHARGE_TX, "status": "timeout"},
            "task {task}: PaymentEscrow.charge is UNCONFIRMED and MAY STILL SETTLE — no settlement was "
            "recorded, so if it does the buyer is charged with NO WAY TO DISPUTE it: status=timeout hash={tx} "
            "(auth {auth}, payer {payer}, 0.050000 USDC, job {job})",
            "charge unconfirmed status=timeout hash={tx} — it may still settle, and this run cannot be disputed",
            "unconfirmed",
        ),
        (
            {"hash": None, "status": None},
            "task {task}: PaymentEscrow.charge is UNCONFIRMED and MAY STILL SETTLE — no settlement was "
            "recorded, so if it does the buyer is charged with NO WAY TO DISPUTE it: status=missing hash=None "
            "(auth {auth}, payer {payer}, 0.050000 USDC, job {job})",
            "charge unconfirmed status=missing hash=None — it may still settle, and this run cannot be disputed",
            "unconfirmed",
        ),
    ],
    ids=["rejected", "timed-out", "no-status"],
)
def test_a_charge_that_did_not_confirm_is_logged_and_traced_with_its_job(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
    line: str,
    trace: str,
    settlement: str,
) -> None:
    chain = _install(monkeypatch, _V1Chain(charge=answer))
    monkeypatch.setattr(execution_svc.secrets, "token_bytes", lambda n: b"\x22" * n)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle() == (None, None, None)

    fields = {"task": TASK, "tx": CHARGE_TX, "auth": AUTH_ID_HEX, "payer": PAYER, "job": (b"\x22" * 16).hex()}
    assert [r.getMessage() for r in caplog.records] == [line.format(**fields)]
    assert _lines() == [("error", trace.format(**fields), settlement)]
    assert [name for name, _ in chain.calls] == ["charge"]
