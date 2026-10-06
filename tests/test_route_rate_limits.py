"""Per-route budgets on the write and expensive endpoints (app/rate_limit.py).

The global limiter spends one budget on every route, sized for dashboard
polling — 1200 a minute. At that rate a single client could start 1200 paid
runs, ask for 1200 Soroban simulations or 1200 registry scans a minute, so
those routes carry a budget of their own: per client, and per wallet where
the request names one. These tests drive the real app, so the budgets are
proven to sit in front of the routes they name, and pin what must NOT change:
the route still reads the whole body, a 413 is still a 413, preflights and
plain reads are never counted, and every refusal is the service's envelope.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from route_inventory import concrete, operator_guard, write_operations

from app import rate_limit
from app.main import app
from app.rate_limit import InMemoryTokenBucket, RoutePolicy

PAYER = "GBWMD26IB6CMG3JO3HU7SD7ZJSTF4BIJ5JS77ANMLJ52M6FV6K3J7BQJ"
OTHER_PAYER = "GDJHP2I6NRCWYZTB3ZOXRE74V4M4EGXRYORGNPTGQ6BVNJNSSJO4PKXJ"
AUTH = "ab" * 16


@pytest.fixture()
def client():
    rate_limit.set_backend(InMemoryTokenBucket())
    with TestClient(app) as c:
        yield c


def _shrink(
    monkeypatch: pytest.MonkeyPatch, name: str, *, per_client: int, per_wallet: int = 0, ceiling: int = 10_000
) -> None:
    """Tighten one policy so a test can cross it in a few requests."""
    policies = []
    for p in rate_limit.POLICIES:
        if p.name == name:
            p = RoutePolicy(p.name, p.methods, p.path, per_client, per_wallet, p.wallet_fields, ceiling)
        policies.append(p)
    monkeypatch.setattr(rate_limit, "POLICIES", tuple(policies))


# Render's own hops, as `security.client_identity` recognises them: one
# Cloudflare edge, then an internal address.
EDGE = "172.70.81.12, 10.201.3.4"


def _from(ip: str) -> dict[str, str]:
    """A request from visitor `ip`, as it reaches us through Render's proxies."""
    return {"x-forwarded-for": f"{ip}, {EDGE}"}


def test_a_route_budget_refuses_past_its_limit_in_the_envelope(client, monkeypatch) -> None:
    _shrink(monkeypatch, "execute", per_client=2)

    codes = [client.post("/api/orchestrator/execute", json={"plan_id": "pln_00000000"}).status_code for _ in range(3)]

    assert codes[:2] == [404, 404]
    assert codes[2] == 429
    r = client.post("/api/orchestrator/execute", json={"plan_id": "pln_00000000"})
    assert r.json()["detail"] == "rate_limited"
    assert r.json()["error"]["code"] == "rate_limited"
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]
    assert int(r.headers["retry-after"]) >= 1
    # The hardening headers wrap the refusal like any other response.
    assert r.headers["x-content-type-options"] == "nosniff"


def test_budgets_are_per_route(client, monkeypatch) -> None:
    _shrink(monkeypatch, "execute", per_client=1)
    client.post("/api/orchestrator/execute", json={"plan_id": "pln_00000000"})
    assert client.post("/api/orchestrator/execute", json={"plan_id": "pln_00000000"}).status_code == 429

    # Another policy's route, and an unlisted read, are untouched.
    assert client.post("/api/payments/x402", json={"agent_id": "agt_01h8", "amount_usdc": 1}).status_code == 402
    assert client.get("/api/agents").status_code == 200


def test_budgets_are_per_client(client, monkeypatch) -> None:
    _shrink(monkeypatch, "execute", per_client=1)
    body = {"plan_id": "pln_00000000"}

    assert client.post("/api/orchestrator/execute", json=body, headers=_from("81.2.69.1")).status_code == 404
    assert client.post("/api/orchestrator/execute", json=body, headers=_from("81.2.69.1")).status_code == 429
    assert client.post("/api/orchestrator/execute", json=body, headers=_from("81.2.69.2")).status_code == 404


def test_a_wallet_cannot_spread_its_spend_across_clients(client, monkeypatch) -> None:
    _shrink(monkeypatch, "stellar_build", per_client=100, per_wallet=2)
    body = {"payer": PAYER, "auth_id_hex": AUTH}

    codes = [
        client.post("/api/stellar/build/reclaim", json=body, headers=_from(f"81.2.69.{i + 1}")).status_code
        for i in range(3)
    ]

    assert 429 not in codes[:2]
    assert codes[2] == 429
    # Another wallet still has its own budget.
    other = client.post("/api/stellar/build/reclaim", json={"payer": OTHER_PAYER, "auth_id_hex": AUTH})
    assert other.status_code != 429


def test_the_route_still_reads_the_whole_body_after_the_wallet_is_sniffed(client, monkeypatch) -> None:
    # A validation error names the field it read, so a body lost in the replay
    # would surface as `payer` missing rather than `auth_id_hex` malformed.
    _shrink(monkeypatch, "stellar_build", per_client=100, per_wallet=100)

    r = client.post("/api/stellar/build/reclaim", json={"payer": PAYER, "auth_id_hex": "not-hex"})

    assert r.status_code == 422
    fields = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert fields == {("body", "auth_id_hex")}


def test_a_wallet_that_is_not_an_address_is_not_a_key(client, monkeypatch) -> None:
    # Junk in the wallet field must not mint a bucket per value; the route's
    # own validation answers it.
    _shrink(monkeypatch, "stellar_build", per_client=100, per_wallet=1)
    for i in range(3):
        r = client.post("/api/stellar/build/reclaim", json={"payer": f"junk{i}", "auth_id_hex": AUTH})
        assert r.status_code == 422


def test_an_oversized_body_is_still_413(client, monkeypatch) -> None:
    _shrink(monkeypatch, "stellar_build", per_client=100, per_wallet=100)

    r = client.post(
        "/api/stellar/build/reclaim",
        content=b'{"payer":"' + b"G" * 2_000_000 + b'"}',
        headers={"content-type": "application/json"},
    )

    assert r.status_code == 413


def test_preflights_are_never_counted(client, monkeypatch) -> None:
    _shrink(monkeypatch, "execute", per_client=1)
    headers = {"Origin": "https://orizons.xyz", "Access-Control-Request-Method": "POST"}
    for _ in range(3):
        client.options("/api/orchestrator/execute", headers=headers)

    assert client.post("/api/orchestrator/execute", json={"plan_id": "pln_00000000"}).status_code == 404


def test_an_expensive_read_has_a_budget_too(client, monkeypatch) -> None:
    # The readiness check probes an operator's endpoint from inside our network.
    _shrink(monkeypatch, "readiness", per_client=1)

    client.get("/api/agents/agt_01h8/readiness")
    assert client.get("/api/agents/agt_01h8/readiness").status_code == 429


_OWN_LIMITER = {"/api/orchestrator/decompose", "/api/pdax/webhooks/receive"}


def _unbudgeted(application) -> list[str]:
    return sorted(
        f"{method} {path}"
        for (method, path), operation in write_operations(application).items()
        if path not in _OWN_LIMITER
        and operator_guard(operation) is None
        and rate_limit.policy_for(method, concrete(path)) is None
    )


def test_every_unguarded_state_changing_route_has_a_budget() -> None:
    """A new write route must decide its budget, not inherit 1200 a minute.

    Routes behind the operator key are exempt (a caller without it is refused
    before any cost), as are two with a limiter of their own: decompose
    (`decompose_rate_limit_per_minute`) and the PDAX webhook, which is HMAC
    verified and must never be throttled against the provider's retries.
    """
    assert _unbudgeted(app) == []


def test_a_new_unbudgeted_route_is_caught() -> None:
    from fastapi import APIRouter, FastAPI

    toy = FastAPI()
    router = APIRouter(prefix="/widgets")

    @router.post("/{widget_id}/spin")
    async def spin(widget_id: str) -> None:
        return None

    toy.include_router(router, prefix="/api")

    assert _unbudgeted(toy) == ["POST /api/widgets/{widget_id}/spin"]


# ── who counts as a client ──────────────────────────────────────


def test_a_caller_nobody_can_attribute_spends_only_the_ceiling(client, monkeypatch) -> None:
    # No per-client budget applies to a chain of infrastructure alone (one
    # shared bucket would let any such caller starve the rest); the route's
    # service-wide ceiling still does.
    _shrink(monkeypatch, "execute", per_client=1, ceiling=3)
    body = {"plan_id": "pln_00000000"}
    anonymous = {"x-forwarded-for": EDGE}

    codes = [client.post("/api/orchestrator/execute", json=body, headers=anonymous).status_code for _ in range(4)]

    assert codes == [404, 404, 404, 429]


def test_the_ceiling_holds_across_every_client(client, monkeypatch) -> None:
    _shrink(monkeypatch, "execute", per_client=100, ceiling=2)
    body = {"plan_id": "pln_00000000"}

    codes = [
        client.post("/api/orchestrator/execute", json=body, headers=_from(f"81.2.69.{i}")).status_code
        for i in range(1, 4)
    ]

    assert codes == [404, 404, 429]


def test_many_visitors_behind_our_frontend_do_not_starve_each_other(client, monkeypatch) -> None:
    # Vercel's egress addresses are shared: the frontend proves itself with
    # FRONTEND_PROXY_TOKEN and names each visitor, so fifty of them polling
    # readiness through it each keep their own budget.
    from app.config import settings

    token = "frontend-proxy-token-" + "k" * 24
    monkeypatch.setattr(settings, "frontend_proxy_token", token)
    _shrink(monkeypatch, "readiness", per_client=1)

    def visit(i: int) -> int:
        headers = {
            "x-forwarded-for": f"76.76.21.9, {EDGE}",
            "x-frontend-proxy-token": token,
            "x-orizon-client-ip": f"81.2.69.{i}",
        }
        return client.get("/api/agents/agt_01h8/readiness", headers=headers).status_code

    assert [visit(i) for i in range(1, 51)] == [200] * 50
    # Each visitor is still held to their own budget.
    assert visit(1) == 429


def test_the_frontends_shared_reads_are_never_held_to_a_client_budget(client, monkeypatch) -> None:
    # A cached route handler reads on everyone's behalf and names no visitor.
    from app.config import settings

    token = "frontend-proxy-token-" + "k" * 24
    monkeypatch.setattr(settings, "frontend_proxy_token", token)
    _shrink(monkeypatch, "readiness", per_client=1)
    headers = {"x-forwarded-for": f"76.76.21.9, {EDGE}", "x-frontend-proxy-token": token}

    assert [client.get("/api/agents/agt_01h8/readiness", headers=headers).status_code for _ in range(5)] == [200] * 5


def test_a_wrong_token_is_just_another_caller(client, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "frontend_proxy_token", "frontend-proxy-token-" + "k" * 24)
    _shrink(monkeypatch, "readiness", per_client=1)
    headers = {
        "x-forwarded-for": f"76.76.21.9, {EDGE}",
        "x-frontend-proxy-token": "guess-" + "k" * 40,
        "x-orizon-client-ip": "81.2.69.7",
    }

    client.get("/api/agents/agt_01h8/readiness", headers=headers)
    headers["x-orizon-client-ip"] = "81.2.69.8"
    # Keyed on Vercel's egress, not on the named visitor: the same client twice.
    assert client.get("/api/agents/agt_01h8/readiness", headers=headers).status_code == 429
