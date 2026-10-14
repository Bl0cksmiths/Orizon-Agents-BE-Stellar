"""Characterization of `_reconcile_seal`'s edges the run-level suite does not reach.

Driven directly, with the verdict, the submission and the registry read
scripted at their seams, so each test pins one branch: a seal that lands while
no settlement record is held, and a cancellation mid-reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from app.schemas import Task
from app.services import execution_svc
from app.services.execution_svc import _PendingSeal, _SealSubmission
from app.state import state
from app.stellar import client as sc

TASK = "tsk_mc_seal"
H1 = "c3" * 32
H2 = "d4" * 32
JOB = bytes(range(16))


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(execution_svc, "SEAL_RECONCILE_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(
        sc,
        "contract_ids",
        lambda: sc.ContractIds(
            agent_registry="",
            reputation_ledger="",
            payment_escrow="",
            attestation_registry="C" + "T" * 55,
            asset_sac="",
        ),
    )
    state.add_task(Task(id=TASK, intent="x", agents=1, spent=0.0, status="complete"))
    yield
    state.tasks.pop(TASK, None)
    state.traces.pop(TASK, None)


def _pending(record: Any = None) -> _PendingSeal:
    return _PendingSeal(
        job_id=JOB,
        args=["arg"],
        agents=2,
        total_usdc=0.03,
        kind="paid",
        submissions=[_SealSubmission(H1, time.time(), "unconfirmed")],
        record=record,
    )


def _proofs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []

    async def _record_proof(task_id: str, record: Any, proof_tx: str) -> None:
        recorded.append(proof_tx)

    monkeypatch.setattr(execution_svc, "_record_proof", _record_proof)
    return recorded


def _messages() -> list[str]:
    return [line.msg for line in state.traces.get(TASK, [])]


def test_a_landed_seal_without_a_record_is_sealed_and_records_no_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _proofs(monkeypatch)

    async def _landed(submission: _SealSubmission) -> str:
        return "landed"

    monkeypatch.setattr(execution_svc, "_seal_verdict", _landed)

    asyncio.run(execution_svc._reconcile_seal(TASK, time.monotonic(), _pending()))

    assert recorded == []
    task = state.tasks[TASK]
    assert (task.seal, task.proof_tx) == ("sealed", H1)
    assert _messages()[0] == f"ERC-8004 attestation sealed · tx {H1[:10]}…"


def test_a_landed_seal_with_a_record_records_its_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _proofs(monkeypatch)

    async def _landed(submission: _SealSubmission) -> str:
        return "landed"

    monkeypatch.setattr(execution_svc, "_seal_verdict", _landed)

    asyncio.run(execution_svc._reconcile_seal(TASK, time.monotonic(), _pending(record=object())))

    assert recorded == [H1]


def test_a_resubmission_that_confirms_without_a_record_records_no_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _proofs(monkeypatch)
    submitted: list[list[Any]] = []

    async def _absent(submission: _SealSubmission) -> str:
        return "absent"

    async def _submit(args: list[Any]) -> tuple[str | None, _SealSubmission | None]:
        submitted.append(args)
        return H2, None

    monkeypatch.setattr(execution_svc, "_seal_verdict", _absent)
    monkeypatch.setattr(execution_svc, "_submit_seal", _submit)
    monkeypatch.setattr(sc, "simulate_read", lambda *a, **k: False)

    pending = _pending()
    asyncio.run(execution_svc._reconcile_seal(TASK, time.monotonic(), pending))

    assert submitted == [["arg"]]
    assert recorded == []
    assert len(pending.submissions) == 1
    assert (state.tasks[TASK].seal, state.tasks[TASK].proof_tx) == ("sealed", H2)


def test_a_cancelled_reconciliation_logs_the_job_and_leaves_the_seal_unwritten(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _cancelled(submission: _SealSubmission) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(execution_svc, "_seal_verdict", _cancelled)

    with (
        caplog.at_level(logging.ERROR, logger="app.services.execution_svc"),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(execution_svc._reconcile_seal(TASK, time.monotonic(), _pending()))

    [line] = [r.getMessage() for r in caplog.records]
    assert line == (
        f"task {TASK}: seal reconciliation cancelled — the seal of job {JOB.hex()} is still unconfirmed "
        f"(submissions: ['{H1}'])"
    )
    assert state.tasks[TASK].seal is None
    assert _messages() == []


def test_an_unreadable_registry_skips_to_the_next_round_and_ends_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdicts: list[str] = []

    async def _absent(submission: _SealSubmission) -> str:
        verdicts.append(submission.tx_hash or "")
        return "absent"

    def _down(*a: Any, **k: Any) -> Any:
        raise RuntimeError("rpc down")

    async def _never(args: list[Any]) -> Any:
        raise AssertionError("must not resubmit while the registry is unreadable")

    monkeypatch.setattr(execution_svc, "_seal_verdict", _absent)
    monkeypatch.setattr(execution_svc, "_submit_seal", _never)
    monkeypatch.setattr(sc, "simulate_read", _down)

    asyncio.run(execution_svc._reconcile_seal(TASK, time.monotonic(), _pending()))

    assert verdicts == [H1, H1]
    assert state.tasks[TASK].seal == "unconfirmed"
    assert _messages() == ["attestation unconfirmed — it may still be on the ledger"]
