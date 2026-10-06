"""Artifact retrieval and the simulated x402 payment flow."""

from __future__ import annotations

from app import money
from app.schemas import Task
from app.state import state


def test_artifact_unknown_task_404(client):
    r = client.get("/api/tasks/tsk_missing/artifact")
    assert r.status_code == 404


def test_artifact_returned_for_stored_task(client):
    task = Task(
        id="tsk_artifact_test",
        intent="build a demo page",
        agents=1,
        spent=0.25,
        status="complete",
        started="just now",
        artifact={"title": "Demo", "files": []},
        charge_tx="charge123",
        proof_tx="proof456",
    )
    state.add_task(task)
    try:
        r = client.get(f"/api/tasks/{task.id}/artifact")
        assert r.status_code == 200
        body = r.json()
        assert body["artifact"] == {"title": "Demo", "files": []}
        assert body["charge_tx"] == "charge123"
        assert body["proof_tx"] == "proof456"
    finally:
        # Leave shared app state exactly as we found it.
        state.tasks.pop(task.id, None)
        state.traces.pop(task.id, None)
        try:
            state.task_order.remove(task.id)
        except ValueError:
            pass


def test_x402_challenges_without_payment_header(client):
    r = client.post(
        "/api/payments/x402",
        json={"agent_id": "agt_01h8", "amount_usdc": 1.5},
    )
    assert r.status_code == 402
    assert r.json() == {"status": "402", "receipt": None}
    # The token is the asset the escrow moves (native XLM on testnet), never an assumed USDC (ADR 0015).
    assert r.headers["x-orizon-payment-required"] == f"amount=1.500;agent=agt_01h8;token={money.asset_code()}"


def test_x402_challenge_amount_keeps_every_decimal(client):
    r = client.post("/api/payments/x402", json={"agent_id": "agt_01h8", "amount_usdc": 0.0125})
    assert r.headers["x-orizon-payment-required"].startswith("amount=0.0125;")


def test_x402_settles_with_payment_header(client):
    r = client.post(
        "/api/payments/x402",
        json={"agent_id": "agt_01h8", "amount_usdc": 1.5},
        headers={"X-Orizon-Payment": "sim-payment"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paid"
    # receipt is "0x" + 10 random bytes hex-encoded
    assert body["receipt"].startswith("0x")
    assert len(body["receipt"]) == 22


def test_the_network_names_the_asset_its_sac_wraps(client, monkeypatch):
    """`asset` used to be the literal "native" whatever SAC was configured; a
    deployment on another asset's SAC would have said native while moving it."""
    from app.config import settings
    from app.stellar import client as sc

    issuer = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
    sac = "CUSDCSACUSDCSACUSDCSACUSDCSACUSDCSACUSDCSACUSDCSACUSDCSA"
    monkeypatch.setattr(settings, "stellar_asset_sac", sac)
    monkeypatch.setattr(money, "_sac_assets", {sac: money.AssetInfo(code="USDC", issuer=issuer)})
    monkeypatch.setattr(sc, "contract_ids", lambda: sc.ContractIds("", "", "", "", sac))

    assert client.get("/api/stellar/network").json()["asset"] == f"USDC:{issuer}"


def test_the_testnet_network_is_native(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "stellar_network_passphrase", "Test SDF Network ; September 2015")
    monkeypatch.setattr(settings, "stellar_asset_sac", money.native_sac_id("Test SDF Network ; September 2015"))

    assert client.get("/api/stellar/network").json()["asset"] == "native"
