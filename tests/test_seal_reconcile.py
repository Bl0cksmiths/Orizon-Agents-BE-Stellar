"""Seal reconciliation: an attestation that did not confirm is checked, and
re-submitted only when it provably is not on the ledger.

The seal is the second transaction of a paid run, after the settle has moved
the money. It used to be submitted once: a seal whose poll ran out, or whose
send was lost, left the job unattested — or attested with nobody knowing —
and nothing ever looked again. Now the run reconciles it, bounded:

  * by the transaction's hash: SUCCESS is the proof; FAILED, or NOT_FOUND once
    the ledger is past the transaction's last valid moment (with the RPC's
    history reaching back before it was sent), is proof it never landed;
  * by the attestation itself: `AttestationRegistry.exists(job_id)`;
  * and re-submits the SAME seal only when every earlier submission is proven
    absent and the job is not attested. That is safe on the contract side:
    `seal` refuses a job id it already holds (`AlreadyExists`, #3), so two
    submissions can never attest one job twice.

The outcome lands on the task (`proof_tx`, `seal`) and in its trace, and the
settlement record gets the proof hash, exactly as a seal that confirmed first
time does.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from stellar_sdk import scval
from test_settle_v2 import SEAL_TX, _auth, _Chain, _install, _Ok, _plan, _run, _Store, _workers

from app.services import dispute_store, execution_svc
from app.state import state
from app.stellar import client as sc

H1 = "a1" * 32
H2 = "b2" * 32


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)
    monkeypatch.setattr(execution_svc, "SEAL_RECONCILE_DELAYS", (0.0, 0.0, 0.0, 0.0))
    yield
    for key in [t for t in state.tasks if t.startswith("tsk_v2_seal_")]:
        state.tasks.pop(key, None)
        state.traces.pop(key, None)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fresh = _Store()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


def _found(tx_hash: str, status: str, *, latest_close: float, oldest_close: float = 0.0) -> sc.LedgerTransaction:
    return sc.LedgerTransaction(
        tx_hash=tx_hash,
        status=status,
        latest_ledger=200,
        latest_ledger_close_time=int(latest_close),
        oldest_ledger=1,
        oldest_ledger_close_time=int(oldest_close),
        ledger=None if status == "NOT_FOUND" else 150,
        envelope_xdr=None,
    )


class _SealChain(_Chain):
    """`_Chain` with a scripted seal, a scripted ledger and a scripted registry.

    `seals` answers each seal submission in turn (a dict, or an exception to
    raise); `lookups` answers `get_transaction` per hash, in turn, the last
    answer repeating; `exists` answers `AttestationRegistry.exists` in turn.
    """

    def __init__(
        self,
        seals: list[Any],
        *,
        lookups: dict[str, list[Any]] | None = None,
        exists: list[Any] | None = None,
    ) -> None:
        super().__init__(auth=_auth())
        self.seals = list(seals)
        self.lookups = lookups or {}
        self.exists_answers = list(exists or [False])
        self.looked_up: list[str] = []

    async def invoke(self, contract_id: str, function_name: str, args: list[Any]) -> dict[str, Any]:
        if function_name == "seal":
            self.calls.append((function_name, args))
            answer = self.seals.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return await super().invoke(contract_id, function_name, args)

    def read(self, contract_id: str, function_name: str, args: Any = None, source: Any = None, **kw: Any) -> Any:
        if function_name == "exists":
            answer = self.exists_answers.pop(0) if len(self.exists_answers) > 1 else self.exists_answers[0]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return super().read(contract_id, function_name, args, source, **kw)

    def lookup(self, tx_hash: str) -> sc.LedgerTransaction:
        self.looked_up.append(tx_hash)
        answers = self.lookups[tx_hash]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _install_seal(monkeypatch: pytest.MonkeyPatch, chain: _SealChain) -> _SealChain:
    _install(monkeypatch, chain)
    monkeypatch.setattr(sc, "get_transaction", chain.lookup)
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Ok()})
    return chain


def _messages(task_id: str) -> list[str]:
    return [line.msg for line in state.traces[task_id]]


def test_a_seal_that_confirms_first_time_is_recorded_as_sealed(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    _install(monkeypatch, _Chain(auth=_auth()))
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Ok()})
    _run(_plan((0.01, 0.02)), "tsk_v2_seal_plain")
    task = state.tasks["tsk_v2_seal_plain"]
    assert (task.proof_tx, task.seal) == (SEAL_TX, "sealed")


def test_an_unconfirmed_seal_found_on_the_ledger_is_sealed_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    now = time.time()
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "timeout"}],
            lookups={H1: [_found(H1, "NOT_FOUND", latest_close=now), _found(H1, "SUCCESS", latest_close=now)]},
        ),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_late")

    assert len(chain.named("seal")) == 1
    task = state.tasks["tsk_v2_seal_late"]
    assert (task.proof_tx, task.seal) == (H1, "sealed")
    assert store.recorded[-1].proof_tx == H1
    assert any(m.startswith("ERC-8004 attestation sealed · tx a1a1") for m in _messages("tsk_v2_seal_late"))


def test_a_rejected_seal_is_resubmitted_identically_and_lands(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """FAILED is the ledger's own word that it did not apply, and the job is
    not attested: the same seal goes out again, under the same job id."""
    now = time.time()
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "FAILED"}, {"hash": H2, "status": "SUCCESS"}],
            lookups={H1: [_found(H1, "FAILED", latest_close=now)]},
            exists=[False],
        ),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_rejected")

    first, second = chain.named("seal")
    assert [a.to_xdr() for a in first] == [a.to_xdr() for a in second]
    task = state.tasks["tsk_v2_seal_rejected"]
    assert (task.proof_tx, task.seal) == (H2, "sealed")
    assert store.recorded[-1].proof_tx == H2


def test_an_expired_lost_seal_is_resubmitted(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """NOT_FOUND, from an RPC whose history covers the submission, on a ledger
    past the transaction's last valid moment: it can never land."""
    later = time.time() + execution_svc.SEAL_TX_TIMEOUT_SECONDS + execution_svc.SEAL_EXPIRY_MARGIN_SECONDS + 5
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [sc.InFlightError("poll failed: dropped", H1), {"hash": H2, "status": "SUCCESS"}],
            lookups={H1: [_found(H1, "NOT_FOUND", latest_close=later)]},
        ),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_expired")

    assert len(chain.named("seal")) == 2
    assert state.tasks["tsk_v2_seal_expired"].proof_tx == H2


def test_a_lost_seal_that_may_still_land_is_never_resubmitted(
    monkeypatch: pytest.MonkeyPatch, store: _Store, caplog: pytest.LogCaptureFixture
) -> None:
    """Inside its validity window NOT_FOUND proves nothing. The reconciliation
    waits out its bound, re-submits nothing, and says the seal is unconfirmed."""
    now = time.time()
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "timeout"}],
            lookups={H1: [_found(H1, "NOT_FOUND", latest_close=now)]},
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        _run(_plan((0.01, 0.02)), "tsk_v2_seal_pending")

    assert len(chain.named("seal")) == 1
    assert chain.looked_up == [H1] * len(execution_svc.SEAL_RECONCILE_DELAYS)
    task = state.tasks["tsk_v2_seal_pending"]
    assert (task.proof_tx, task.seal, task.settlement) == (None, "unconfirmed", "settled")
    assert any("attestation unconfirmed" in m for m in _messages("tsk_v2_seal_pending"))
    assert any("seal UNCONFIRMED after reconciliation" in r.getMessage() for r in caplog.records)


def test_a_history_that_starts_after_the_seal_proves_nothing(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    later = time.time() + 600
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "timeout"}],
            lookups={H1: [_found(H1, "NOT_FOUND", latest_close=later, oldest_close=later - 10)]},
        ),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_gap")

    assert len(chain.named("seal")) == 1
    assert state.tasks["tsk_v2_seal_gap"].seal == "unconfirmed"


def test_a_job_the_registry_already_holds_is_sealed_and_never_resubmitted(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    """The contract's duplicate rule, from either side: a seal refused as
    AlreadyExists, or a job `exists` answers for, IS attested."""
    chain = _install_seal(
        monkeypatch,
        _SealChain([sc.ContractError("simulate failed: HostError: Error(Contract, #3)", 3)], exists=[True]),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_exists")

    assert len(chain.named("seal")) == 1
    task = state.tasks["tsk_v2_seal_exists"]
    assert (task.seal, task.proof_tx) == ("sealed", None)
    assert "attestation found on-chain for this job" in _messages("tsk_v2_seal_exists")


def test_resubmission_is_bounded_and_ends_failed(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    now = time.time()
    rejected = [{"hash": f"{i:02x}" * 32, "status": "FAILED"} for i in range(10)]
    chain = _install_seal(
        monkeypatch,
        _SealChain(rejected, lookups={r["hash"]: [_found(r["hash"], "FAILED", latest_close=now)] for r in rejected}),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_failed")

    assert len(chain.named("seal")) == execution_svc.MAX_SEAL_SUBMISSIONS
    task = state.tasks["tsk_v2_seal_failed"]
    assert (task.seal, task.proof_tx, task.status) == ("failed", None, "complete")
    assert any("attestation not sealed" in m for m in _messages("tsk_v2_seal_failed"))


def test_an_unreadable_ledger_leaves_it_unconfirmed_and_the_run_completes(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [sc.InFlightError("send raised", H1)],
            lookups={H1: [RuntimeError("rpc down")]},
            exists=[RuntimeError("rpc down")],
        ),
    )

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_blind")

    assert len(chain.named("seal")) == 1
    task = state.tasks["tsk_v2_seal_blind"]
    assert (task.seal, task.status, task.charge_tx is not None) == ("unconfirmed", "complete", True)


def test_the_seal_names_the_job_the_settle_paid(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    now = time.time()
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "FAILED"}, {"hash": H2, "status": "SUCCESS"}],
            lookups={H1: [_found(H1, "FAILED", latest_close=now)]},
        ),
    )
    _run(_plan((0.01, 0.02)), "tsk_v2_seal_job")
    [settle] = chain.named("settle")
    assert {scval.to_native(s[1]) for s in chain.named("seal")} == {scval.to_native(settle[2])}


def test_the_validity_window_reasoned_about_is_the_one_the_client_builds() -> None:
    """`SEAL_TX_TIMEOUT_SECONDS` is the seal's last valid moment only while the
    client builds server-signed transactions with that very timeout."""
    import inspect

    source = inspect.getsource(sc._send_server_signed)
    assert f".set_timeout({execution_svc.SEAL_TX_TIMEOUT_SECONDS})" in source


# ── the receipt the console reads (`GET /api/tasks/{id}/disputes`) ──────
def test_the_receipt_carries_the_seal_state_and_the_proof_it_found(
    monkeypatch: pytest.MonkeyPatch, store: _Store, client: Any
) -> None:
    now = time.time()
    _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "FAILED"}, {"hash": H2, "status": "SUCCESS"}],
            lookups={H1: [_found(H1, "FAILED", latest_close=now)]},
        ),
    )
    _run(_plan((0.01, 0.02)), "tsk_v2_seal_receipt")

    receipt = client.get("/api/tasks/tsk_v2_seal_receipt/disputes").json()

    assert (receipt["seal"], receipt["seal_kind"], receipt["proof_tx"]) == ("sealed", "paid", H2)
    assert receipt["settlement"]["proof_tx"] == H2


def test_the_receipt_says_when_the_seal_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, store: _Store, client: Any
) -> None:
    now = time.time()
    _install_seal(
        monkeypatch,
        _SealChain([{"hash": H1, "status": "timeout"}], lookups={H1: [_found(H1, "NOT_FOUND", latest_close=now)]}),
    )
    _run(_plan((0.01, 0.02)), "tsk_v2_seal_receipt_open")

    receipt = client.get("/api/tasks/tsk_v2_seal_receipt_open/disputes").json()

    assert (receipt["seal"], receipt["proof_tx"], receipt["settlement_state"]) == ("unconfirmed", None, "settled")


def test_without_the_task_the_receipt_reads_the_seal_off_the_settlement() -> None:
    """After a restart without the task (no DATABASE_URL), the settlement
    record is all there is: a proof hash on it IS a confirmed seal."""
    from dataclasses import replace

    from app.routers import disputes
    from app.services.dispute_store import SettlementRecord, SettlementStep

    record = SettlementRecord(
        task_id="tsk_v2_seal_gone",
        payer="G" + "A" * 55,
        auth_id_hex="ab" * 16,
        job_id_hex="cd" * 16,
        charge_tx="ef" * 32,
        proof_tx=H1,
        settled_usdc=0.01,
        steps=(SettlementStep(0, "agt_0", "w.agt_0", 0.01, True),),
        settled_at=1.0,
        window_closes_at=2.0,
    )
    assert disputes._receipt_seal("tsk_v2_seal_gone", record) == ("sealed", "paid", H1)
    # A sealed run that moved nothing was attested as delivery only.
    unpaid = replace(record, settled_usdc=0.0)
    assert disputes._receipt_seal("tsk_v2_seal_gone", unpaid) == ("sealed", "delivery_only", H1)
    assert disputes._receipt_seal("tsk_v2_seal_gone", replace(record, proof_tx=None)) == (None, None, None)
    assert disputes._receipt_seal("tsk_v2_seal_gone", None) == (None, None, None)


# ── delivery-only seals (nobody could be paid) ──────────────────────────
def test_a_delivery_only_seal_is_reconciled_like_a_paid_one(
    monkeypatch: pytest.MonkeyPatch, store: _Store, client: Any
) -> None:
    """Rejected once, provably absent, re-submitted identically — still with no
    receipt and a zero total — and labelled delivery-only on the task, in the
    trace and on the receipt."""
    now = time.time()
    chain = _install_seal(
        monkeypatch,
        _SealChain(
            [{"hash": H1, "status": "FAILED"}, {"hash": H2, "status": "SUCCESS"}],
            lookups={H1: [_found(H1, "FAILED", latest_close=now)]},
        ),
    )
    chain.owners = {"agt_0": None, "agt_1": None}

    _run(_plan((0.01, 0.02)), "tsk_v2_seal_delivery")

    first, second = chain.named("seal")
    assert [a.to_xdr() for a in first] == [a.to_xdr() for a in second]
    assert (scval.to_native(second[5]), scval.to_native(second[6])) == ([], 0)
    task = state.tasks["tsk_v2_seal_delivery"]
    assert (task.seal, task.seal_kind, task.proof_tx) == ("sealed", "delivery_only", H2)
    assert any(
        m.startswith("workflow sealed — 2 agents delivered, no payment was made")
        for m in _messages("tsk_v2_seal_delivery")
    )
    receipt = client.get("/api/tasks/tsk_v2_seal_delivery/disputes").json()
    assert (receipt["seal"], receipt["seal_kind"], receipt["proof_tx"]) == ("sealed", "delivery_only", H2)
    assert receipt["settlement_state"] == "settled" and receipt["settlement"]["settled_usdc"] == 0
