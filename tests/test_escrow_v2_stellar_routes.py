"""The escrow-v2 edges of routers/stellar.py (story 5.01, ADR 0011): the authorize TTL and the v1-only charge."""

from __future__ import annotations

import time
from typing import Any

import pytest
from escrow_fakes import ESCROW, FakeEscrow, install, use_escrow
from fastapi.testclient import TestClient
from stellar_sdk import Keypair

from app.config import settings
from app.routers.stellar import AuthorizeReq
from app.services import authorization_guard as guard
from app.stellar import client as sc

PAYER = Keypair.from_raw_ed25519_seed(bytes(range(32))).public_key
# The worst-case run `/execute` requires an authorization to outlive on v2: the
# reputation re-check, 125 s for each of the planner's six steps, and the settle.
WORST_SIX_STEP_RUN_SECONDS = 2.5 + 6 * 125 + 150


@pytest.fixture(autouse=True)
def escrow(monkeypatch: pytest.MonkeyPatch) -> None:
    use_escrow(monkeypatch, ESCROW)
    guard.forget_versions()
    yield
    guard.forget_versions()


def test_the_default_ttl_outlives_the_longest_plans_worst_case_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "build_invoke_xdr", lambda *a, **k: "AAAA")
    before = int(time.time())
    r = client.post(
        "/api/stellar/build/authorize", json={"payer": PAYER, "agent_id": "pln_0a1b2c3d", "max_amount_usdc": 1}
    )
    assert r.status_code == 200, r.text
    ttl = r.json()["expires_at"] - before
    assert 1800 <= ttl <= 1802
    assert ttl > WORST_SIX_STEP_RUN_SECONDS + 600  # and ten minutes for the wallet, the confirm and the execute


def test_the_ttl_bound_is_unchanged() -> None:
    assert AuthorizeReq.model_fields["ttl_seconds"].default == 1800
    with pytest.raises(ValueError):
        AuthorizeReq(payer=PAYER, agent_id="pln_0a1b2c3d", max_amount_usdc=1, ttl_seconds=3601)


class Invokes:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(args)
        return {"status": "SUCCESS", "hash": "0" * 64}


@pytest.fixture()
def charge_ready(monkeypatch: pytest.MonkeyPatch) -> Invokes:
    """A deployment that could sign a charge: a key, an operator API key, and a recorded invoke."""
    invokes = Invokes()
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    monkeypatch.setattr(settings, "api_key", "op-key")
    monkeypatch.setattr(sc, "invoke_with_server_key_async", invokes)
    return invokes


def charge(client: TestClient) -> Any:
    return client.post(
        "/api/stellar/server/charge",
        json={"auth_id_hex": "ab" * 16, "amount_usdc": 0.01, "job_id_hex": "cd" * 16},
        headers={"X-API-Key": "op-key"},
    )


def test_a_charge_against_v2_is_refused_with_a_pointer_to_settle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, charge_ready: Invokes
) -> None:
    install(monkeypatch, FakeEscrow(version=2))
    r = charge(client)
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "charge_unsupported_on_v2" and body["error"]["code"] == "charge_unsupported_on_v2"
    assert "settle" in body["error"]["message"]
    assert charge_ready.calls == []


def test_a_charge_against_v1_is_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, charge_ready: Invokes
) -> None:
    install(monkeypatch, FakeEscrow(version=None))
    r = charge(client)
    assert r.status_code == 200, r.text
    [(contract_id, function_name, _args)] = charge_ready.calls
    assert (contract_id, function_name) == (ESCROW, "charge")


def test_an_unreadable_version_leaves_the_charge_to_the_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, charge_ready: Invokes
) -> None:
    """Safe to let through: v2 has no `charge`, so its simulation refuses before anything is signed or sent."""
    install(monkeypatch, FakeEscrow(version=ConnectionError("rpc down")))
    assert charge(client).status_code == 200
    assert len(charge_ready.calls) == 1


def test_the_key_and_cap_checks_still_come_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, charge_ready: Invokes
) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=2))
    monkeypatch.setattr(settings, "stellar_signing_key", "")
    assert charge(client).status_code == 503
    assert escrow.calls == []
