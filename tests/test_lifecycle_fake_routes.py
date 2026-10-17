"""Characterization of the lifecycle harness's in-memory world, route by route.

`FakeWorld` is the double the whole lifecycle suite runs against. The runs
exercise its happy paths; this file pins the edges they never reach — the
not-found answers, the routes a knob turns off, the simulation failures —
so a change to how the fake dispatches cannot quietly change an answer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from stellar_sdk import Account, Keypair, TransactionBuilder, scval
from stellar_sdk import xdr as stellar_xdr

from scripts.lifecycle.config import TESTNET_PASSPHRASE
from scripts.lifecycle.fakes import (
    AGENT,
    AGENT_2,
    AGENT_2_NAME,
    AGENT_NAME,
    API,
    ATTEST,
    ESCROW,
    HORIZON,
    LEDGER,
    RPC,
    SAC,
    FakeWorld,
)

BUYER = Keypair.random().public_key


def _world(**knobs: Any) -> FakeWorld:
    return FakeWorld(buyer=BUYER, **knobs)


def _send(world: FakeWorld, method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    with httpx.Client(transport=world.transport()) as client:
        return client.request(method, url, json=body, headers=headers)


def _err(code: str) -> dict[str, Any]:
    return {"detail": code, "error": {"code": code, "message": code, "request_id": "req_x"}}


# ── the API's fixed routes ──────────────────────────────────────
@pytest.mark.parametrize(
    ("method", "path", "code"),
    [
        ("GET", "/api/orchestrator/decompose", "not_found"),
        ("GET", "/api/disputes", "not_found"),
        ("GET", "/api/nowhere", "not_found"),
        ("GET", "/api/stellar/agent/ghost", "agent_read_failed"),
        ("GET", f"/api/stellar/agent/{AGENT_2}", "agent_read_failed"),
        ("GET", "/api/tasks/tsk_x", "unknown_task"),
        ("GET", "/api/trace/tsk_x", "unknown_task"),
        ("GET", "/api/disputes/dsp_x", "unknown_dispute"),
        ("POST", "/api/disputes/dsp_x/uphold", "invalid_api_key"),
    ],
)
def test_unknown_things_answer_their_error(method: str, path: str, code: str) -> None:
    response = _send(_world(), method, API + path)
    status = 401 if code == "invalid_api_key" else 404
    assert (response.status_code, response.json()) == (status, _err(code))


def test_an_unknown_tasks_dispute_listing_is_empty() -> None:
    response = _send(_world(), "GET", API + "/api/tasks/tsk_x/disputes")
    assert (response.status_code, response.json()) == (
        200,
        {"task_id": "tsk_x", "window_closes_at": None, "now": 1_900_000_500.0, "settlement": None, "disputes": []},
    )


def test_the_read_grant_routes_are_absent_without_d067() -> None:
    world = _world(d067=False)
    for path in ("/api/disputes/read-challenge", "/api/disputes/read-grant"):
        response = _send(world, "POST", API + path, {"task_id": "tsk_x"})
        assert (response.status_code, response.json()) == (404, _err("not_found"))


def test_the_read_challenge_answers_with_d067() -> None:
    body = _send(_world(), "POST", API + "/api/disputes/read-challenge", {"task_id": "tsk_x"}).json()
    assert body["message"] == f"orizon-dispute-read:v1:tsk_x:{body['nonce']}"
    assert body["expires_at"] == 1.0


def test_health_wakes_after_its_failures() -> None:
    world = _world(health_failures=1)
    first = _send(world, "GET", API + "/api/health")
    assert (first.status_code, first.text) == (503, "<html>waking</html>")
    second = _send(world, "GET", API + "/api/health")
    assert (second.status_code, second.json()) == (200, {"status": "ok", "version": "t", "uptime_seconds": 1.0})


def test_readiness_answers_unless_behind_the_proxy() -> None:
    world = _world()
    answer = _send(world, "GET", API + "/readiness")
    assert (answer.status_code, answer.json()) == (
        200,
        {"status": "ready", "ratings": {"writer": "scorer", "signer": world.settler, "scorer": world.settler}},
    )
    world.readiness_reachable = False
    hidden = _send(world, "GET", API + "/readiness")
    assert (hidden.status_code, hidden.text) == (404, "<html>not found</html>")


@pytest.mark.parametrize("passphrase", [TESTNET_PASSPHRASE, "Some Other Network"])
def test_the_network_names_every_contract(passphrase: str) -> None:
    world = _world(passphrase=passphrase)
    assert _send(world, "GET", API + "/api/stellar/network").json() == {
        "network": "testnet" if passphrase == TESTNET_PASSPHRASE else "mainnet",
        "rpc_url": RPC,
        "network_passphrase": passphrase,
        "admin": world.settler,
        "dispatch_signer": None,
        "asset": "native",
        "asset_sac": SAC,
        "contracts": {
            "agent_registry": "CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ",
            "reputation_ledger": LEDGER,
            "payment_escrow": ESCROW,
            "attestation_registry": ATTEST,
        },
    }


def _row(agent_id: str, name: str, skill: str, price: float, owner: str | None, source: str, bound: Any) -> Any:
    return {
        "id": agent_id,
        "name": name,
        "skills": [skill],
        "price": price,
        "rep": 4.0,
        "status": "online",
        "runs": 0,
        "owner": owner,
        "source": source,
        "bound": bound,
    }


@pytest.mark.parametrize("second", [False, True])
def test_the_agents_list_the_external_agents_then_the_seeded_one(second: bool) -> None:
    world = _world(second_agent=second, agent_bound=False, agent_price=0.3, seeded_price=0.1)
    expected = [
        _row(AGENT, AGENT_NAME, "code", 0.3, world.owner, "onchain", False),
        *([_row(AGENT_2, AGENT_2_NAME, "review", 0.3, world.owner_2, "onchain", True)] if second else []),
        _row("agt_writer", "Writer", "copy", 0.1, None, "seeded", None),
    ]
    assert _send(world, "GET", API + "/api/agents").json() == expected


def test_the_registry_reads_name_each_owner() -> None:
    world = _world(second_agent=True)
    assert _send(world, "GET", f"{API}/api/stellar/agent/{AGENT}").json() == {
        "agent": {"owner": world.owner, "name": AGENT_NAME}
    }
    assert _send(world, "GET", f"{API}/api/stellar/agent/{AGENT_2}").json() == {
        "agent": {"owner": world.owner_2, "name": AGENT_2_NAME}
    }


def test_the_reputation_batch_carries_the_agents_record() -> None:
    world = _world()
    assert _send(world, "GET", API + "/api/stellar/reputation").json() == {
        "reputations": {AGENT: world.rep},
        "floor_bps": 5500,
        "prior_bps": 7000,
    }


# ── dispatch ────────────────────────────────────────────────────
def test_every_request_is_logged_and_a_crash_is_raised() -> None:
    world = _world(crash_on="/api/agents")
    with pytest.raises(RuntimeError, match="fake crash on /api/agents"):
        _send(world, "POST", API + "/api/agents", {"x": 1})
    assert world.calls[-1]["method"] == "POST"
    assert world.calls[-1]["json"] == {"x": 1}
    assert world.posted_bodies == ['{"x":1}']


def test_an_unknown_host_answers_599() -> None:
    response = _send(_world(), "GET", "https://elsewhere.fake/x")
    assert (response.status_code, response.text) == (599, "no fake for https://elsewhere.fake/x")


def test_horizon_answers_only_known_transactions() -> None:
    world = _world()
    assert _send(world, "GET", HORIZON + "/transactions/abc").json() == {"status": 404}
    assert _send(world, "GET", HORIZON + "/accounts/abc").status_code == 404


def test_the_rpc_refuses_unknown_methods() -> None:
    body = _send(_world(), "POST", RPC, {"jsonrpc": "2.0", "id": 3, "method": "getFeeStats"}).json()
    assert body == {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "no"}}


# ── simulation ──────────────────────────────────────────────────
def _simulate(world: FakeWorld, contract: str, function: str, args: list[stellar_xdr.SCVal]) -> dict[str, Any]:
    tx = (
        TransactionBuilder(Account(BUYER, 0), network_passphrase=world.passphrase, base_fee=100)
        .append_invoke_contract_function_op(contract_id=contract, function_name=function, parameters=args)
        .set_timeout(30)
        .build()
    )
    return world.simulate(tx.to_xdr())


def _value(world: FakeWorld, val: stellar_xdr.SCVal) -> dict[str, Any]:
    return {"results": [{"xdr": str(val.to_xdr()), "auth": []}], "latestLedger": world.ledger}


def _fail(world: FakeWorld, message: str) -> dict[str, Any]:
    return {"error": message, "latestLedger": world.ledger}


def test_simulation_answers_each_view_or_its_failure() -> None:
    v1 = _world(escrow_version=1)
    missing = "HostError: Error(WasmVm, MissingValue)"
    assert _simulate(v1, ESCROW, "version", []) == _fail(v1, missing)
    assert _simulate(v1, ESCROW, "settler", []) == _fail(v1, missing)
    v2 = _world()
    assert _simulate(v2, ESCROW, "version", []) == _value(v2, scval.to_uint32(2))
    assert _simulate(v2, ESCROW, "settler", []) == _value(v2, scval.to_address(v2.settler))
    absent = scval.to_bytes(bytes(16))
    assert _simulate(v2, ESCROW, "authorization", [absent]) == _fail(v2, "HostError: Error(Contract, #2)")
    assert _simulate(v2, ATTEST, "get", [absent]) == _fail(v2, "HostError: Error(Contract, #2)")
    assert _simulate(v2, SAC, "balance", [scval.to_address(BUYER)]) == _value(v2, scval.to_int128(v2.balances[BUYER]))
    stranger = scval.to_address(Keypair.random().public_key)
    assert _simulate(v2, SAC, "balance", [stranger]) == _fail(
        v2, "HostError: Error(Contract, #6)\n\nEvent log (newest first): account entry is missing"
    )
    assert _simulate(v2, LEDGER, "get", []) == _fail(v2, f"no fake for {LEDGER}.get")
    assert _simulate(v2, ESCROW, "charge", []) == _fail(v2, f"no fake for {ESCROW}.charge")
