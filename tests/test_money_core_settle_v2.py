"""Characterization of `_settle_v2`'s branches the run-level suite does not reach.

Each test drives `_settle_v2` directly against `test_settle_v2._Chain`, so the
real payout builder, owner reads and seal composition run and nothing reaches
the network. What is pinned is what the caller and the buyer see: the return
value, the chain calls in order, the trace lines and the task's settlement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from stellar_sdk import scval
from test_settle_v2 import (
    AUTH_ID_HEX,
    ESCROW,
    PAYER,
    SETTLE_TX,
    _add_task,
    _auth,
    _Chain,
    _install,
    _payouts,
    _plan,
    _receipt,
)

from app.config import settings
from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

TASK = "tsk_mc_v2"


@pytest.fixture(autouse=True)
def _task() -> Any:
    _add_task(TASK)
    yield
    state.tasks.pop(TASK, None)
    state.traces.pop(TASK, None)


def _settle(
    plan_prices: tuple[float, ...],
    delivered: frozenset[int],
    **kwargs: Any,
) -> tuple[str | None, str | None, bytes | None]:
    return asyncio.run(
        execution_svc._settle_v2(
            TASK,
            time.monotonic(),
            _plan(plan_prices),
            payer=PAYER,
            auth_id_hex=AUTH_ID_HEX,
            delivered_steps=delivered,
            **kwargs,
        )
    )


def _lines() -> list[tuple[str, str, str | None]]:
    return [(line.level, line.msg, line.settlement) for line in state.traces.get(TASK, [])]


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_without_a_signing_key_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(settings, "stellar_signing_key", "")

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle((0.01,), frozenset({0})) == (None, None, None)

    assert chain.calls == []
    assert chain.reads == []
    assert _lines() == [("error", "STELLAR_SIGNING_KEY not set — skipping on-chain settle/seal", "failed")]
    assert _errors(caplog) == [
        f"task {TASK}: STELLAR_SIGNING_KEY not set — skipping on-chain settle/seal "
        f"(auth {AUTH_ID_HEX}, payer {PAYER}, 1 steps delivered)"
    ]
    assert state.tasks[TASK].settlement == "failed"


@pytest.mark.parametrize(
    ("auth", "trace"),
    [
        (RuntimeError("not a record"), "the authorization is unreadable"),
        (None, "the authorization was not found"),
    ],
    ids=["unreadable", "not-found"],
)
def test_an_authorization_that_cannot_be_read_is_refused_and_released(
    monkeypatch: pytest.MonkeyPatch, auth: Any, trace: str
) -> None:
    chain = _install(monkeypatch, _Chain(auth=auth))

    assert _settle((0.01,), frozenset({0})) == (None, None, None)

    [release] = chain.named("settle")
    assert _payouts(release) == []
    assert _lines()[0] == ("error", f"settlement refused before submitting — {trace}", "failed")
    assert chain.reads == [(ESCROW, "authorization")]


def test_payouts_that_cannot_be_built_are_refused_and_released(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth(max_amount=10**12)))

    assert _settle(tuple(0.01 for _ in range(17)), frozenset(range(17))) == (None, None, None)

    [release] = chain.named("settle")
    assert _payouts(release) == []
    assert _lines()[0] == ("error", "settlement refused before submitting — the payouts could not be built", "failed")


def test_a_receipt_count_that_does_not_match_the_payouts_seals_without_links(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settle = {"hash": SETTLE_TX, "status": "SUCCESS", "result": [_receipt(0)]}
    chain = _install(monkeypatch, _Chain(auth=_auth(), settle=settle))
    settled: list[Any] = []

    async def on_settled(tx: str, job_id: bytes, payout_plan: Any, receipts: list[bytes] | None) -> None:
        settled.append((tx, receipts))

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        settle_tx, proof_tx, job_id = _settle((0.01, 0.02), frozenset({0, 1}), on_settled=on_settled)

    assert settle_tx == SETTLE_TX
    assert proof_tx is not None
    assert job_id is not None
    assert settled == [(SETTLE_TX, None)]
    [seal] = chain.named("seal")
    assert scval.to_native(seal[5]) == []
    assert ("error", "settle receipt ids missing — sealing attestation without receipt links", None) in _lines()
    assert any(m.startswith(f"task {TASK}: settle result did not decode to 2 receipt ids") for m in _errors(caplog))


def test_a_rejected_release_is_not_released_again(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth(), settle={"hash": SETTLE_TX, "status": "FAILED"}))

    assert _settle((0.01,), frozenset()) == (None, None, None)

    assert [name for name, _ in chain.calls] == ["settle"]
    assert _lines() == [("error", f"settle status=FAILED hash={SETTLE_TX}", "failed")]


def test_a_release_refused_before_sending_is_not_released_again(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth(), settle=sc.NotSubmittedError("signer refused")))

    assert _settle((0.01,), frozenset()) == (None, None, None)

    assert [name for name, _ in chain.calls] == ["settle"]
    assert _lines() == [("error", "on-chain settlement failed", "failed")]


def test_a_paid_settle_refused_before_sending_releases_the_custody(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def invoke(contract_id: str, function_name: str, args: list[Any]) -> dict[str, Any]:
        calls.append(_payouts(args))
        if len(calls) == 1:
            raise sc.NotSubmittedError("signer refused")
        return {"hash": SETTLE_TX, "status": "SUCCESS", "result": []}

    _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(sc, "invoke_with_server_key_async", invoke)

    assert _settle((0.01,), frozenset({0})) == (None, None, None)

    assert calls == [[{"agent_id": "agt_0", "amount": 100_000}], []]
    assert _lines() == [("error", "on-chain settlement failed", "failed")]


def test_an_unconfirmed_seal_with_nobody_to_hand_it_to_still_returns_the_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Chain(auth=_auth(), seal={"hash": "5a" * 32, "status": "timeout"}))

    settle_tx, proof_tx, job_id = _settle((0.01,), frozenset({0}))

    assert (settle_tx, proof_tx) == (SETTLE_TX, None)
    assert job_id is not None
    assert state.tasks[TASK].seal == "pending"
    assert _lines()[-1] == (
        "error",
        f"seal unconfirmed hash={'5a' * 32} — checking the ledger before trying again",
        None,
    )


def test_an_unconfirmed_seal_is_logged_and_handed_to_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth(), seal={"hash": "5a" * 32, "status": "FAILED"}))
    handed: list[execution_svc._PendingSeal] = []

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        settle_tx, _, job_id = _settle((0.01, 0.02), frozenset({0, 1}), on_seal_pending=handed.append)

    assert job_id is not None
    [pending] = handed
    [seal] = chain.named("seal")
    assert (pending.job_id, pending.agents, pending.kind, pending.record) == (job_id, 2, "paid", None)
    assert pending.total_usdc == pytest.approx(0.03)
    assert [a.to_xdr() for a in pending.args] == [a.to_xdr() for a in seal]
    assert [(s.tx_hash, s.outcome) for s in pending.submissions] == [("5a" * 32, "rejected")]
    assert _errors(caplog) == [
        f"task {TASK}: AttestationRegistry.seal did not confirm — rejected, hash={'5a' * 32}; reconciling "
        f"(job {job_id.hex()} settled 300000 stroops via tx {settle_tx}, auth {AUTH_ID_HEX}, payer {PAYER})"
    ]


def test_a_failure_after_the_settle_confirmed_keeps_its_hash_and_settlement(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))

    async def on_settled(*args: Any) -> None:
        raise RuntimeError("store exploded")

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        settle_tx, proof_tx, job_id = _settle((0.01,), frozenset({0}), on_settled=on_settled)

    assert (settle_tx, proof_tx) == (SETTLE_TX, None) and job_id is not None
    assert [name for name, _ in chain.calls] == ["settle"]
    assert _lines()[-1] == ("error", "on-chain settlement failed", None)
    assert state.tasks[TASK].settlement == "settled"
    [failure] = [r for r in caplog.records if "on-chain settle failed" in r.getMessage()]
    assert failure.exc_info is not None
    assert failure.getMessage() == (
        f"task {TASK}: on-chain settle failed: store exploded (job {job_id.hex()}, auth {AUTH_ID_HEX}, "
        f"payer {PAYER}, 100000 stroops, settle_tx={SETTLE_TX}, proof_tx=None)"
    )


def test_a_release_that_confirms_is_traced_as_released(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))

    settle_tx, proof_tx, job_id = _settle((0.01,), frozenset())

    assert (settle_tx, proof_tx, job_id) == (SETTLE_TX, None, execution_svc.unsettled_job_id(TASK))
    assert [name for name, _ in chain.calls] == ["settle"]
    assert chain.reads == []
    assert _lines() == [
        ("cost", f"x402 settle → nothing paid, custody released to the buyer · tx {SETTLE_TX[:10]}…", "released")
    ]


@pytest.mark.parametrize(
    ("answer", "line", "settlement", "calls"),
    [
        (
            {"hash": SETTLE_TX, "status": "FAILED"},
            "task {task}: PaymentEscrow.settle did not settle — status=FAILED hash={tx} "
            "(auth {auth}, payer {payer}, 100000 stroops, job {job})",
            "failed",
            2,
        ),
        (
            {"hash": SETTLE_TX, "status": ""},
            "task {task}: PaymentEscrow.settle is UNCONFIRMED and MAY STILL SETTLE — no settlement was "
            "recorded, so if it does the buyer is charged with NO WAY TO DISPUTE it: status=missing hash={tx} "
            "(auth {auth}, payer {payer}, 100000 stroops, job {job})",
            "unconfirmed",
            1,
        ),
    ],
    ids=["rejected", "unconfirmed"],
)
def test_a_settle_that_did_not_confirm_is_logged_with_its_job(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
    line: str,
    settlement: str,
    calls: int,
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth(), settle=answer))
    monkeypatch.setattr(execution_svc.secrets, "token_bytes", lambda n: b"\x11" * n)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle((0.01,), frozenset({0})) == (None, None, None)

    job = (b"\x11" * 16).hex()
    assert _errors(caplog)[0] == line.format(task=TASK, tx=SETTLE_TX, auth=AUTH_ID_HEX, payer=PAYER, job=job)
    assert len(chain.named("settle")) == calls
    assert state.tasks[TASK].settlement == settlement


def test_an_unreadable_owner_is_logged_with_its_traceback_and_left_unpaid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, _Chain(owners={"ext_op": RuntimeError("rpc down")}))

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        owners = asyncio.run(execution_svc._settle_owners({"ext_op"}))

    assert (owners.payees, owners.unpaid) == ({}, {"ext_op": execution_svc.UNPAID_OWNER_UNREADABLE})
    [record] = caplog.records
    assert record.getMessage() == "owner of ext_op unreadable before settle — its steps are not paid: rpc down"
    assert record.exc_info is not None and record.exc_info[1] is not None
