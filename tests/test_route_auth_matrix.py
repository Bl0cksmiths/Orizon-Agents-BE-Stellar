"""Every state-changing route, and what authorizes it — one table, checked against the app.

The audit behind this file walked every non-GET route. Each one is either
behind the operator key, or authorized by something the caller cannot forge
(a wallet signature over a server challenge, an on-chain authorization, an
HMAC), or deliberately public because it changes nothing a caller does not
already control. The table says which, with the reason, and the tests make
it a contract: a new write route fails here until someone decides which it is,
and an operator-keyed route that loses its dependency fails here too. The
routes and their guards are read off the published schema (route_inventory.py
says why), so the check holds on every FastAPI the lock or the range allows.

The signature and authorization checks themselves are exercised where they
live (tests/test_money_route_auth.py, test_bind_api*.py, test_unbind_api.py,
test_dispute_read_grant.py, test_execute_guard_route.py, test_pdax_auth.py).
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from route_inventory import Guard, concrete, operator_guard, write_operations

from app.main import app
from app.security import require_api_key

OPERATOR_KEY_FAIL_CLOSED = "operator key, fail closed (503 while API_KEY is unset)"
OPERATOR_KEY = "operator key (X-API-Key); open while API_KEY is unset, which config refuses on mainnet"
WALLET_SIGNATURE = "wallet signature over a single-use server challenge, checked against the on-chain owner/payer"
PUBLIC_CHALLENGE = "public: mints a single-use challenge from a capped budget; changes nothing by itself"
PUBLIC_UNSIGNED = "public: returns unsigned XDR; the wallet must sign it and the contract checks require_auth"

AUTH: dict[tuple[str, str], str] = {
    ("POST", "/api/agents/{agent_id}/bind/challenge"): PUBLIC_CHALLENGE,
    ("POST", "/api/agents/{agent_id}/unbind/challenge"): PUBLIC_CHALLENGE,
    ("POST", "/api/agents/{agent_id}/bind"): WALLET_SIGNATURE,
    ("DELETE", "/api/agents/{agent_id}/bind"): WALLET_SIGNATURE,
    ("POST", "/api/orchestrator/decompose"): "public: builds a plan, moves nothing; own LLM budget per client",
    ("POST", "/api/orchestrator/execute"): (
        "paid: the payer's on-chain escrow authorization for this plan (authorization_guard); "
        "simulated: public, charges nobody and writes no rating"
    ),
    ("POST", "/api/disputes/challenge"): PUBLIC_CHALLENGE,
    ("POST", "/api/disputes/read-challenge"): PUBLIC_CHALLENGE,
    ("POST", "/api/disputes/read-grant"): WALLET_SIGNATURE,
    ("POST", "/api/disputes"): WALLET_SIGNATURE,
    ("POST", "/api/disputes/{dispute_id}/uphold"): OPERATOR_KEY_FAIL_CLOSED,
    ("POST", "/api/disputes/{dispute_id}/reject"): OPERATOR_KEY_FAIL_CLOSED,
    ("POST", "/api/payments/x402"): "public: a simulated 402 handshake; stores and moves nothing",
    ("POST", "/api/stellar/agents/sync"): "public: re-reads the public registry, single-flight and rate-limited",
    ("POST", "/api/stellar/reputation/{agent_id}/invalidate"): OPERATOR_KEY_FAIL_CLOSED,
    ("POST", "/api/stellar/build/register-agent"): PUBLIC_UNSIGNED,
    ("POST", "/api/stellar/build/update-price"): PUBLIC_UNSIGNED,
    ("POST", "/api/stellar/build/set-active"): PUBLIC_UNSIGNED,
    ("POST", "/api/stellar/build/authorize"): PUBLIC_UNSIGNED,
    ("POST", "/api/stellar/build/reclaim"): PUBLIC_UNSIGNED,
    ("POST", "/api/stellar/submit"): "public: relays an envelope the caller already signed; the chain verifies it",
    ("POST", "/api/stellar/server/charge"): OPERATOR_KEY,
    ("POST", "/api/stellar/server/seal"): OPERATOR_KEY_FAIL_CLOSED,
    ("POST", "/api/pdax/webhooks/receive"): "HMAC over the body with PDAX_WEBHOOK_SECRET",
    ("POST", "/api/pdax/trade/quote"): OPERATOR_KEY,
    ("POST", "/api/pdax/trade/quote/v2"): OPERATOR_KEY,
    ("POST", "/api/pdax/trade/order"): OPERATOR_KEY,
    ("POST", "/api/pdax/fiat/deposit"): OPERATOR_KEY,
    ("POST", "/api/pdax/fiat/withdraw"): OPERATOR_KEY,
    ("POST", "/api/pdax/fiat/user-info-upload"): OPERATOR_KEY,
    ("POST", "/api/pdax/crypto/withdraw"): OPERATOR_KEY,
    ("POST", "/api/pdax/webhooks/register"): OPERATOR_KEY,
    ("POST", "/api/pdax/ramp/estimate"): OPERATOR_KEY,
    ("POST", "/api/pdax/ramp/funding-quote"): OPERATOR_KEY,
    ("POST", "/api/pdax/ramp/onramp"): OPERATOR_KEY,
    ("POST", "/api/pdax/ramp/offramp"): OPERATOR_KEY,
    ("POST", "/api/pdax/ramp/{ramp_id}/reconcile"): OPERATOR_KEY,
}

_KEY_GUARDS: dict[str, Guard] = {
    OPERATOR_KEY: "api_key",
    OPERATOR_KEY_FAIL_CLOSED: "operator_key_fail_closed",
}

KEYED = [k for k, v in AUTH.items() if v in _KEY_GUARDS]
UNKEYED = [k for k, v in AUTH.items() if v not in _KEY_GUARDS]


def _undeclared(application: FastAPI) -> list[tuple[str, str]]:
    return sorted(set(write_operations(application)) - set(AUTH))


def test_every_state_changing_route_has_a_declared_authorization() -> None:
    routes = set(write_operations(app))

    assert _undeclared(app) == [], "a new write route needs an entry in AUTH, and a decision"
    assert sorted(set(AUTH) - routes) == [], "a route in AUTH no longer exists; drop it"


def test_a_new_write_route_in_an_included_router_is_caught() -> None:
    # The check's own teeth, on the shape that broke the old walk: a route
    # that arrives through a prefixed, included router.
    toy = FastAPI()
    inner = APIRouter(prefix="/things")

    @inner.post("/{thing_id}/poke")
    async def poke(thing_id: str) -> None:
        return None

    toy.include_router(inner, prefix="/api")

    assert _undeclared(toy) == [("POST", "/api/things/{thing_id}/poke")]


def test_a_router_level_key_dependency_is_seen_on_each_route() -> None:
    # PDAX puts `require_api_key` on the router, not on each route.
    toy = FastAPI()
    secured = APIRouter(dependencies=[Depends(require_api_key)])

    @secured.post("/withdraw")
    async def withdraw() -> None:
        return None

    toy.include_router(secured, prefix="/api/money")

    assert operator_guard(write_operations(toy)[("POST", "/api/money/withdraw")]) == "api_key"


@pytest.mark.parametrize("key", KEYED, ids=lambda k: f"{k[0]} {k[1]}")
def test_an_operator_keyed_route_carries_its_guard(key: tuple[str, str]) -> None:
    operation = write_operations(app)[key]

    assert operator_guard(operation) == _KEY_GUARDS[AUTH[key]], f"{key} lost its operator-key dependency"


@pytest.mark.parametrize("key", KEYED, ids=lambda k: f"{k[0]} {k[1]}")
def test_an_operator_keyed_route_refuses_a_caller_without_the_key(client, hermetic_settings, key) -> None:
    # The schema says the guard is there; this says it runs.
    hermetic_settings.api_key = "operator-secret-key"
    method, path = key

    r = client.request(method, concrete(path), json={})

    assert (r.status_code, r.json()["detail"]) == (401, "invalid_api_key")


@pytest.mark.parametrize("key", UNKEYED, ids=lambda k: f"{k[0]} {k[1]}")
def test_a_route_not_behind_the_key_is_not_silently_put_behind_it(key: tuple[str, str]) -> None:
    # Moving a buyer-facing route behind the operator key would lock every
    # buyer out of it; that has to be a decision made in AUTH, not a side effect.
    assert operator_guard(write_operations(app)[key]) is None
