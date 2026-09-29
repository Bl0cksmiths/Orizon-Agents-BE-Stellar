"""An in-memory Soroban RPC and Horizon for the evidence tool's hermetic suite.

`FakeLedger` holds transactions by hash and answers `getNetwork`,
`getTransaction`, Horizon's root and `/transactions/{hash}` from them through
one `httpx.MockTransport`. A transaction can be visible to the RPC (inside its
retention window), to Horizon only (older), or to neither.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import TESTNET_PASSPHRASE

RPC = "https://rpc.test"
HORIZON = "https://horizon.test"
MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"


def tx_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


@dataclass
class FakeTx:
    status: str  # SUCCESS | FAILED
    ledger: int
    rpc_visible: bool = True
    horizon_visible: bool = True
    created_at: str = "2026-09-24T12:00:00Z"


@dataclass
class FakeLedger:
    rpc_passphrase: str = TESTNET_PASSPHRASE
    horizon_passphrase: str = TESTNET_PASSPHRASE
    rpc_down: bool = False  # getTransaction answers 503 (getNetwork still answers)
    horizon_down: bool = False  # /transactions answers 503 (the root still answers)
    txs: dict[str, FakeTx] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def add(
        self,
        tag: str,
        status: str = "SUCCESS",
        *,
        rpc: bool = True,
        horizon: bool = True,
        created_at: str = "2026-09-24T12:00:00Z",
    ) -> str:
        h = tx_hash(tag)
        self.txs[h] = FakeTx(status, 1000 + len(self.txs), rpc, horizon, created_at)
        return h

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(RPC):
            payload = json.loads(request.content)
            method, params = payload.get("method"), payload.get("params") or {}
            self.calls.append(f"rpc {method}")

            def ok(result: Any) -> httpx.Response:
                return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

            if method == "getNetwork":
                return ok({"passphrase": self.rpc_passphrase})
            if method == "getTransaction":
                if self.rpc_down:
                    return _json(503, {"error": "unavailable"})
                tx = self.txs.get(params["hash"])
                if tx is None or not tx.rpc_visible:
                    return ok({"status": "NOT_FOUND", "latestLedger": 5000})
                return ok({"status": tx.status, "ledger": tx.ledger, "latestLedger": 5000})
            return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601}})
        if url.startswith(HORIZON):
            path = request.url.path
            self.calls.append(f"horizon {request.method} {path}")
            if path == "/":
                return _json(200, {"network_passphrase": self.horizon_passphrase})
            if path.startswith("/transactions/"):
                if self.horizon_down:
                    return _json(503, {"status": 503})
                tx = self.txs.get(path.split("/")[2])
                if tx is None or not tx.horizon_visible:
                    return _json(404, {"status": 404})
                return _json(
                    200, {"successful": tx.status == "SUCCESS", "ledger": tx.ledger, "created_at": tx.created_at}
                )
            return _json(404, {"status": 404})
        raise AssertionError(f"unexpected request {request.method} {url}")


def row(
    event: str,
    tx: str | None,
    *,
    stage: str | None = None,
    network: str = "testnet",
    seq: int = 1,
    agent: str | None = "alpha",
    amount: str | None = None,
    status: str | None = "SUCCESS",
) -> dict[str, Any]:
    """One evidence row in the lifecycle harness's shape (`scripts/lifecycle/evidence.EvidenceRow`)."""
    return {
        "stage": stage or event,
        "event": event,
        "utc": "2026-09-29T10:00:00Z",
        "network": network,
        "run_id": "run-1",
        "tx_hash": tx,
        "explorer": f"https://stellar.expert/explorer/testnet/tx/{tx}" if tx else None,
        "contract": None,
        "agent": agent,
        "buyer": None,
        "amount": amount,
        "asset": "XLM" if amount else None,
        "onchain_status": status if tx else None,
        "detail": {"summary": f"{event} row"},
        "seq": seq,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]], *, torn_tail: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    path.write_text(text + (torn_tail or ""))
    return path
