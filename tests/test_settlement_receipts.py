"""The attestation seal must carry the receipt id PaymentEscrow.charge minted —
the receipts vector is the attestation's only on-chain link to the payment that
funded it — and the on-chain settlement path must never fire for a run that
produced nothing: a charge for zero delivered work consumes the payer's escrow
authorization for nothing. Also covers the per-step contract that a worker
returning a non-dict fails that step, not the run."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from stellar_sdk import Keypair, scval

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

AUTH_ID_HEX = "ab" * 16
SIGNING_SECRET = Keypair.random().secret
PAYER = Keypair.random().public_key
# BytesN<16> receipt id in the exact form client._finalize_invoke emits it:
# the decoded bytes have been converted with .hex(), so a 32-char hex string.
RECEIPT_HEX = "cd" * 16


@pytest.fixture(autouse=True)
def clean_state():
    yield
    for task_id in [t for t in state.traces if t.startswith("tsk_receipt_")]:
        state.traces.pop(task_id, None)
    for task_id in [t for t in state.tasks if t.startswith("tsk_receipt_")]:
        state.tasks.pop(task_id, None)


def _plan(agent_ids: tuple[str, ...] = ("agt_x",)) -> StoredPlan:
    return StoredPlan(
        id="pln_receipt",
        intent="settle something",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id=agent_id,
                    agent_name=f"w.{agent_id}",
                    rationale="r",
                    est_price_usdc=0.05,
                    est_eta_seconds=1.0,
                )
                for agent_id in agent_ids
            ]
        ),
        total_usdc=0.05 * len(agent_ids),
        total_eta=1.0,
    )


def _add_task(task_id: str) -> None:
    state.add_task(
        Task(
            id=task_id,
            intent="settle something",
            agents=1,
            spent=0.0,
            status="running",
            started="just now",
        )
    )


def _settle(task_id: str) -> tuple[str | None, str | None, bytes | None]:
    return asyncio.run(
        execution_svc._settle_onchain(
            task_id,
            time.monotonic(),
            _plan(),
            payer=PAYER,
            auth_id_hex=AUTH_ID_HEX,
            total_usdc=0.05,
        )
    )


def _errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.services.execution_svc" and r.levelno >= logging.ERROR]


def _use_fake_signer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "stellar_signing_key", SIGNING_SECRET)
    monkeypatch.setattr(sc, "_signer_keypair", lambda: Keypair.from_secret(SIGNING_SECRET))


def _invoke_recording_seal(seal_calls: list, charge_payload: dict):
    """Fake invoke boundary in the REAL client shape: _finalize_invoke returns
    {"hash", "status", "ledger", "result"} — NOT "return_value", which only
    _finalize_submit (the user-signed path) uses."""

    async def fake_invoke(contract_id, function_name, args):
        if function_name == "charge":
            return charge_payload
        seal_calls.append(args)
        return {"hash": "sealhash456", "status": "SUCCESS", "ledger": 2, "result": None}

    return fake_invoke


# ── receipt propagation into the seal ───────────────────────────────────
def test_seal_receives_the_decoded_charge_receipt(monkeypatch):
    _use_fake_signer(monkeypatch)
    seal_calls: list = []
    monkeypatch.setattr(
        sc,
        "invoke_with_server_key_async",
        _invoke_recording_seal(
            seal_calls,
            {"hash": "chargehash123", "status": "SUCCESS", "ledger": 1, "result": RECEIPT_HEX},
        ),
    )

    charge_tx, proof_tx, job_id = _settle("tsk_receipt_ok")

    assert (charge_tx, proof_tx) == ("chargehash123", "sealhash456")
    assert job_id is not None
    assert len(seal_calls) == 1
    # seal args: settler, job_id, payer, intent_hash, agents, receipts, total
    receipts_vec = seal_calls[0][5]
    assert receipts_vec == scval.to_vec([sc.bytes16(bytes.fromhex(RECEIPT_HEX))])


def test_receipt_under_the_submit_paths_key_is_not_read(monkeypatch, caplog):
    """Mutation guard: a receipt offered only under "return_value" — the old,
    wrong key — must NOT reach the seal. Reverting the lookup to
    charge.get("return_value") makes this payload produce a receipt and the
    empty-vector assertion below fail."""
    _use_fake_signer(monkeypatch)
    seal_calls: list = []
    monkeypatch.setattr(
        sc,
        "invoke_with_server_key_async",
        _invoke_recording_seal(
            seal_calls,
            {"hash": "chargehash123", "status": "SUCCESS", "ledger": 1, "return_value": RECEIPT_HEX},
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        _settle("tsk_receipt_wrongkey")

    assert seal_calls[0][5] == scval.to_vec([])
    assert any("receipt id" in r.getMessage() for r in _errors(caplog))


@pytest.mark.parametrize("bad_result", ["ab" * 12, "zz" * 16, None])
def test_malformed_receipt_seals_without_link_and_logs(monkeypatch, caplog, bad_result):
    """Wrong length, non-hex, or missing: the seal still lands (paid work must
    be attested) but the dropped receipt link is logged, never silent."""
    _use_fake_signer(monkeypatch)
    seal_calls: list = []
    monkeypatch.setattr(
        sc,
        "invoke_with_server_key_async",
        _invoke_recording_seal(
            seal_calls,
            {"hash": "chargehash123", "status": "SUCCESS", "ledger": 1, "result": bad_result},
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        charge_tx, proof_tx, job_id = _settle("tsk_receipt_bad")

    assert (charge_tx, proof_tx) == ("chargehash123", "sealhash456")
    assert job_id is not None
    assert seal_calls[0][5] == scval.to_vec([])
    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any(
        "tsk_receipt_bad" in m and "receipt id" in m and "chargehash123" in m and job_id.hex() in m for m in msgs
    ), f"a dropped receipt link was never logged with its context: {msgs}"
    lines = state.traces["tsk_receipt_bad"]
    assert any(ln.level == "error" and "receipt" in ln.msg for ln in lines)


# ── settlement gating: a run that produced nothing must not settle ──────
class _BoomWorker:
    def __init__(self, name: str = "w.boom") -> None:
        self.name = name

    async def run(self, intent, rationale, context=None):
        raise RuntimeError("openai: invalid_api_key")


class _OkWorker:
    def __init__(self, name: str = "w.ok", *, artifact: bool = False) -> None:
        self.name = name
        self._artifact = artifact

    async def run(self, intent, rationale, context=None):
        output = {"summary": "did the thing"}
        if self._artifact:
            output["artifact"] = {
                "title": "Demo",
                "files": [{"path": "index.html", "language": "html", "content": "<p>hi</p>"}],
            }
        return output


class _StringWorker:
    name = "w.str"

    async def run(self, intent, rationale, context=None):
        return "not a dict"


def _resolves_to(monkeypatch, lookup) -> None:
    """Pin the dispatch seam to `lookup`.

    execution_svc resolves a step's worker through the async `resolve_worker`
    (local registry first, then a stored binding); these tests only care which
    worker comes back, so a plain agent_id -> Worker|None lookup is adapted to
    that seam here rather than at every call site.
    """

    async def _resolve(agent_id):
        return lookup(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _patch_settlement_recorders(monkeypatch) -> tuple[list, list]:
    settle_calls: list = []
    rating_calls: list = []

    async def fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        settle_calls.append((payer, auth_id_hex, total_usdc))
        return ("chargehash123", "sealhash456", b"\x01" * 16)

    async def fake_ratings(task_id, start, plan, context, *, payer, job_id):
        rating_calls.append(job_id)

    monkeypatch.setattr(execution_svc, "_settle_onchain", fake_settle)
    monkeypatch.setattr(execution_svc, "_submit_ratings", fake_ratings)
    return settle_calls, rating_calls


def test_run_with_no_successful_step_skips_charge_seal_and_ratings(monkeypatch):
    """Every step failed: nothing was delivered, so the payer's escrow
    authorization must not be consumed — no charge, no seal, no ratings."""
    _resolves_to(monkeypatch, lambda agent_id: _BoomWorker())
    settle_calls, rating_calls = _patch_settlement_recorders(monkeypatch)
    task_id = "tsk_receipt_allfail"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan(("agt_x", "agt_y")), task_id, auth_id_hex=AUTH_ID_HEX, payer=PAYER))

    assert settle_calls == []
    assert rating_calls == []
    task = state.tasks[task_id]
    assert task.status == "failed"
    assert task.charge_tx is None and task.proof_tx is None
    assert any("skipping on-chain charge/seal" in ln.msg for ln in state.traces[task_id])


def test_partial_run_still_settles_on_chain(monkeypatch):
    """Degraded but delivered: some steps succeeded and were billed, so the
    charge/seal/ratings sequence runs exactly as before the gate."""
    workers = {"agt_x": _OkWorker("w.gen", artifact=True), "agt_y": _BoomWorker("w.critic")}
    _resolves_to(monkeypatch, workers.get)
    settle_calls, rating_calls = _patch_settlement_recorders(monkeypatch)
    task_id = "tsk_receipt_partial"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan(("agt_x", "agt_y")), task_id, auth_id_hex=AUTH_ID_HEX, payer=PAYER))

    assert settle_calls == [(PAYER, AUTH_ID_HEX, pytest.approx(0.05))]
    assert rating_calls == [b"\x01" * 16]
    task = state.tasks[task_id]
    assert task.charge_tx == "chargehash123" and task.proof_tx == "sealhash456"
    assert not any("skipping on-chain" in ln.msg for ln in state.traces[task_id])


# ── a worker returning a non-dict fails that step, not the run ──────────
def test_non_dict_worker_output_fails_that_step_only(monkeypatch, caplog):
    _resolves_to(monkeypatch, lambda agent_id: _StringWorker())
    task_id = "tsk_receipt_nondict"
    _add_task(task_id)

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        asyncio.run(execution_svc._run(_plan(("agt_x",)), task_id))

    task = state.tasks[task_id]
    assert task.status == "failed"
    assert task.spent == 0.0  # an unusable result is never billed
    lines = state.traces[task_id]
    # Handled by the per-step path, not the run-level except:
    assert not any("workflow failed" in ln.msg for ln in lines)
    assert any(ln.level == "error" and "w.str returned an unusable result" in ln.msg for ln in lines)
    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any(task_id in m and "agt_x" in m and "str" in m for m in msgs)


def test_non_dict_worker_output_does_not_sink_a_delivering_run(monkeypatch):
    workers = {"agt_x": _OkWorker("w.gen", artifact=True), "agt_y": _StringWorker()}
    _resolves_to(monkeypatch, workers.get)
    task_id = "tsk_receipt_nondict_mixed"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan(("agt_x", "agt_y")), task_id))

    task = state.tasks[task_id]
    assert task.status == "complete"  # partial with an artifact — delivered
    assert task.artifact is not None
    assert task.spent == 0.05  # only the step that produced output was billed
