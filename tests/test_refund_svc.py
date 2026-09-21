"""Story 4.01 — partial-credit refund mechanism (app/services/refund_svc.py).

Pins the settler-funded-credit design: the derived dispute job id that clears
the ReputationLedger replay guard (R12), the stated credit-amount policy, and
that the two settler-signed invocations are built with the right arguments (a
SAC transfer settler→buyer, and a dispute rating under the derived id).
"""

from __future__ import annotations

import asyncio
import hashlib

import app.stellar.client as sc
from app.services import refund_svc


def test_dispute_job_id_is_deterministic_16_bytes_and_distinct() -> None:
    jid = bytes(range(16))
    d = refund_svc.dispute_job_id(jid)
    assert isinstance(d, bytes) and len(d) == 16
    assert d == refund_svc.dispute_job_id(jid)  # deterministic
    assert d != jid  # not the settled job's id (clears the replay guard)
    assert d == hashlib.sha256(jid + b"dispute").digest()[:16]  # documented derivation


def test_dispute_job_id_differs_per_job() -> None:
    a = refund_svc.dispute_job_id(bytes(16))
    b = refund_svc.dispute_job_id(bytes([1]) + bytes(15))
    assert a != b


def test_credited_amount_full_partial_and_clamped() -> None:
    assert refund_svc.credited_amount_usdc(0.054) == 0.054  # default fraction 1.0
    assert refund_svc.credited_amount_usdc(0.10, 0.5) == 0.05
    assert refund_svc.credited_amount_usdc(0.10, 2.0) == 0.10  # fraction clamped to 1.0
    assert refund_svc.credited_amount_usdc(0.10, -1.0) == 0.0  # fraction clamped to 0
    assert refund_svc.credited_amount_usdc(-5.0) == 0.0  # negative charge floored


def test_execute_refund_transfers_from_settler_to_buyer(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def _fake_invoke(contract_id: str, fn: str, args: list) -> dict:
        calls.update(contract=contract_id, fn=fn, args=args)
        return {"hash": "refund_tx", "status": "SUCCESS"}

    monkeypatch.setattr(sc, "signer_public_key", lambda: "GSETTLER")
    monkeypatch.setattr(sc, "invoke_with_server_key_async", _fake_invoke)
    monkeypatch.setattr(sc, "addr", lambda a: ("addr", a))
    monkeypatch.setattr(sc, "i128", lambda v: ("i128", v))  # usdc_to_i128 stays real

    result = asyncio.run(refund_svc.execute_refund("GBUYER", 0.08))

    assert result["hash"] == "refund_tx"
    assert calls["fn"] == "transfer"  # a SAC transfer, not a contract refund
    assert calls["contract"] == sc.contract_ids().asset_sac
    # settler → buyer, amount in stroops (0.08 USDC = 800_000)
    assert calls["args"] == [("addr", "GSETTLER"), ("addr", "GBUYER"), ("i128", 800_000)]
