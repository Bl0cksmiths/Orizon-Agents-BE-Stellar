"""Characterization tests for the evidence tool's fake RPC and Horizon (`FakeLedger`).

The run tests reach its happy paths; these pin every answer it gives,
including the ones the tool never asks for: an unknown RPC method, an unknown
Horizon path, and a host that is neither.
"""

from __future__ import annotations

import httpx
import pytest

from scripts.demo_evidence.fakes import HORIZON, RPC, FakeLedger


def _rpc(ledger: FakeLedger, method: str, params: dict[str, str] | None = None) -> httpx.Response:
    with httpx.Client(transport=ledger.transport()) as client:
        return client.post(RPC, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params})


def _horizon(ledger: FakeLedger, path: str) -> httpx.Response:
    with httpx.Client(transport=ledger.transport()) as client:
        return client.get(HORIZON + path)


def test_rpc_answers() -> None:
    ledger = FakeLedger(rpc_passphrase="rpc net")
    seen = ledger.add("seen", "FAILED")
    hidden = ledger.add("hidden", rpc=False)
    assert _rpc(ledger, "getNetwork").json() == {"jsonrpc": "2.0", "id": 7, "result": {"passphrase": "rpc net"}}
    assert _rpc(ledger, "getTransaction", {"hash": seen}).json()["result"] == {
        "status": "FAILED",
        "ledger": 1000,
        "latestLedger": 5000,
    }
    for h in (hidden, "f" * 64):
        assert _rpc(ledger, "getTransaction", {"hash": h}).json()["result"] == {
            "status": "NOT_FOUND",
            "latestLedger": 5000,
        }
    unknown = _rpc(ledger, "getLedgers")
    assert (unknown.status_code, unknown.json()) == (200, {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601}})
    ledger.rpc_down = True
    down = _rpc(ledger, "getTransaction", {"hash": seen})
    assert (down.status_code, down.json()) == (503, {"error": "unavailable"})
    assert _rpc(ledger, "getNetwork").status_code == 200
    assert ledger.calls == ["rpc getNetwork", *["rpc getTransaction"] * 3, "rpc getLedgers"] + [
        "rpc getTransaction",
        "rpc getNetwork",
    ]


def test_horizon_answers() -> None:
    ledger = FakeLedger(horizon_passphrase="horizon net")
    ok = ledger.add("ok", created_at="2026-09-25T00:00:00Z")
    failed = ledger.add("failed", "FAILED")
    hidden = ledger.add("hidden", horizon=False)
    assert _horizon(ledger, "/").json() == {"network_passphrase": "horizon net"}
    assert _horizon(ledger, f"/transactions/{ok}").json() == {
        "successful": True,
        "ledger": 1000,
        "created_at": "2026-09-25T00:00:00Z",
    }
    assert _horizon(ledger, f"/transactions/{failed}").json()["successful"] is False
    for path in (f"/transactions/{hidden}", "/transactions/" + "f" * 64, "/ledgers/1"):
        missing = _horizon(ledger, path)
        assert (missing.status_code, missing.json()) == (404, {"status": 404})
    ledger.horizon_down = True
    down = _horizon(ledger, f"/transactions/{ok}")
    assert (down.status_code, down.json()) == (503, {"status": 503})
    assert _horizon(ledger, "/").status_code == 200
    assert ledger.calls[0] == "horizon GET /"
    assert ledger.calls[-3] == "horizon GET /ledgers/1"


def test_any_other_host_is_an_assertion() -> None:
    with httpx.Client(transport=FakeLedger().transport()) as client:
        with pytest.raises(AssertionError, match=r"^unexpected request GET https://elsewhere\.test/x$"):
            client.get("https://elsewhere.test/x")
