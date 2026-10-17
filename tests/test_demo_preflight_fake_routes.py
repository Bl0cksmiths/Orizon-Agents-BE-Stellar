"""Characterization of the demo pre-flight's in-memory deployment, route by route.

`FakeWorld` is the double every pre-flight test runs against, so each route's
status, body and call-log line is pinned here: a change to the fake that
changes what a route answers shows up as a failure in this file first.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts.demo_preflight.fakes import (
    AGENT_ENDPOINT,
    API,
    BACKEND,
    FLOOR,
    FRONTEND,
    HORIZON,
    RPC,
    FakeWorld,
    healthy_world,
    ready_steps,
)

NOT_FOUND = {"detail": "Not Found", "error": {"code": "not_found"}}


def _send(world: FakeWorld, method: str, url: str, body: Any = None) -> httpx.Response:
    with httpx.Client(transport=world.transport()) as client:
        if body is None:
            return client.request(method, url)
        return client.request(method, url, json=body)


def _answer(response: httpx.Response) -> tuple[int, Any]:
    return response.status_code, response.json()


# ── the API ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/agents", (404, NOT_FOUND)),
        ("GET", "/api/orchestrator/decompose", (404, NOT_FOUND)),
        ("GET", "/api/nowhere", (404, NOT_FOUND)),
        ("GET", "/api/agents/", (404, NOT_FOUND)),
    ],
)
def test_api_unknown_routes_answer_not_found(method: str, path: str, expected: tuple[int, Any]) -> None:
    world = healthy_world()
    assert _answer(_send(world, method, API + path)) == expected
    assert world.calls == [f"api {method} {path}"]


def test_api_decompose_answers_the_configured_plan() -> None:
    world = healthy_world()
    world.decompose_status = 502
    assert _answer(_send(world, "POST", API + "/api/orchestrator/decompose", {"intent": "x"})) == (
        502,
        world.decompose_body,
    )


def test_api_health_wakes_after_its_failures() -> None:
    world = healthy_world()
    world.health_failures = 2
    answers = [_answer(_send(world, "GET", API + "/api/health")) for _ in range(3)]
    assert answers == [(503, {"error": "waking"}), (503, {"error": "waking"}), (200, {"status": "ok"})]
    world.health_down = True
    assert _answer(_send(world, "GET", API + "/api/health")) == (503, {"error": "waking"})


def test_api_reads_answer_the_worlds_state() -> None:
    world = healthy_world()
    world.api_passphrase = "elsewhere"
    assert _answer(_send(world, "GET", API + "/api/stellar/network")) == (
        200,
        {**world.network, "network_passphrase": "elsewhere"},
    )
    assert _answer(_send(world, "GET", API + "/api/ecosystem/adoption")) == (200, world.adoption)
    assert _answer(_send(world, "GET", API + "/api/agents")) == (200, world.agents)
    assert _answer(_send(world, "GET", API + "/api/stellar/reputation/params")) == (200, world.params)
    assert _answer(_send(world, "GET", API + "/api/stellar/reputation")) == (
        200,
        {"reputations": world.reputations, "floor_bps": FLOOR, "prior_bps": 7000},
    )
    world.adoption = None
    assert _answer(_send(world, "GET", API + "/api/ecosystem/adoption")) == (404, NOT_FOUND)


def test_api_degraded_batches_count_down() -> None:
    world = healthy_world()
    world.degraded_batches = 1
    status, body = _answer(_send(world, "GET", API + "/api/stellar/reputation"))
    assert status == 200
    assert world.degraded_batches == 0
    assert body["reputations"]["alpha"] == {
        **world.reputations["alpha"],
        "lower_bound_bps": 5677,
        "source": "prior",
        "degraded": True,
    }
    assert _send(world, "GET", API + "/api/stellar/reputation").json()["reputations"] == world.reputations


def test_api_agent_readiness_answers_known_unknown_and_absent_routes() -> None:
    world = healthy_world()
    assert _answer(_send(world, "GET", API + "/api/agents/alpha/readiness")) == (
        200,
        world.agent_readiness["alpha"],
    )
    assert _answer(_send(world, "GET", API + "/api/agents/ghost/readiness")) == (
        200,
        {"agent_id": "ghost", "checked_at": 1, "ready": False, "steps": ready_steps(registered="todo")},
    )
    world.readiness_route = False
    assert _answer(_send(world, "GET", API + "/api/agents/alpha/readiness")) == (404, NOT_FOUND)


# ── the backend host, the frontend and the agent ────────────────
def test_backend_readiness_answers_its_report_with_the_ready_status() -> None:
    world = healthy_world()
    assert world.readiness is not None
    assert _answer(_send(world, "GET", BACKEND + "/readiness")) == (200, world.readiness)
    world.readiness["status"] = "not_ready"
    assert _answer(_send(world, "GET", BACKEND + "/readiness")) == (503, world.readiness)
    assert _answer(_send(world, "GET", BACKEND + "/other")) == (404, {"detail": "Not Found"})
    world.readiness = None
    assert _answer(_send(world, "GET", BACKEND + "/readiness")) == (404, {"detail": "Not Found"})
    assert world.calls == ["backend GET /readiness"] * 2 + ["backend GET /other", "backend GET /readiness"]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_frontend_redirects_to_the_login(status: int) -> None:
    world = healthy_world()
    world.pages["/app"] = status
    response = _send(world, "GET", FRONTEND + "/app")
    assert response.status_code == status
    assert response.headers["location"] == f"{FRONTEND}/login"
    assert world.calls == ["frontend GET /app"]


def test_frontend_pages_answer_their_status() -> None:
    world = healthy_world()
    world.pages["/broken"] = 500
    broken = _send(world, "GET", FRONTEND + "/broken")
    assert (broken.status_code, broken.text) == (500, "<html></html>")
    missing = _send(world, "GET", FRONTEND + "/x")
    assert (missing.status_code, missing.text) == (404, "<html></html>")


def test_agent_endpoint_answers_its_health_and_headers() -> None:
    world = healthy_world()
    world.agent_status = 418
    world.agent_headers = {"X-Fault-Injection": "slow"}
    response = _send(world, "GET", AGENT_ENDPOINT + "/dispatch?token=t")
    assert _answer(response) == (418, world.agent_health)
    assert response.headers["x-fault-injection"] == "slow"
    assert world.calls == ["agent GET /dispatch?token=t"]


# ── the RPC and Horizon ─────────────────────────────────────────
def _rpc(world: FakeWorld, method: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        payload["params"] = params
    return _answer(_send(world, "POST", RPC, payload))


def test_rpc_answers_its_network_and_refuses_other_methods() -> None:
    world = healthy_world()
    assert _rpc(world, "getNetwork") == (
        200,
        {"jsonrpc": "2.0", "id": 7, "result": {"passphrase": world.rpc_passphrase, "protocolVersion": 23}},
    )
    assert _rpc(world, "getLedgerEntries") == (
        200,
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "no"}},
    )
    assert world.calls == ["rpc getNetwork", "rpc getLedgerEntries"]
    world.rpc_down = True
    assert _rpc(world, "getNetwork") == (503, {"error": "unavailable"})


def test_rpc_simulation_down_answers_503() -> None:
    world = healthy_world()
    world.simulate_down = True
    assert _rpc(world, "simulateTransaction", {"transaction": "AAAA"}) == (503, {"error": "unavailable"})


def test_horizon_answers_its_root_accounts_and_nothing_else() -> None:
    world = healthy_world()
    assert _answer(_send(world, "GET", HORIZON + "/")) == (200, {"network_passphrase": world.horizon_passphrase})
    account = next(iter(world.accounts))
    assert _answer(_send(world, "GET", f"{HORIZON}/accounts/{account}")) == (200, world.accounts[account])
    assert _answer(_send(world, "GET", f"{HORIZON}/accounts/GNOBODY")) == (404, {"status": 404})
    assert _answer(_send(world, "GET", f"{HORIZON}/ledgers")) == (404, {"status": 404})
    assert world.calls[-1] == "horizon GET /ledgers"


def test_an_unexpected_host_is_an_assertion() -> None:
    world = healthy_world()
    with pytest.raises(AssertionError, match="unexpected request GET https://elsewhere.test/x"):
        _send(world, "GET", "https://elsewhere.test/x")
    assert world.calls == []
