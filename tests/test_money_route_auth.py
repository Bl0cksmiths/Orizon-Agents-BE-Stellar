"""Every money-moving / account-revealing route stays behind the API key.

The secured PDAX routes and the Stellar server-signed routes inherit
`require_api_key` from a shared sub-router, which is correct but easy to break
silently: a future refactor that moves one route back onto the public router
would pass every other test. This pins the invariant directly — with an API key
configured, each of these routes must answer 401 to an unauthenticated call —
so such a regression fails loudly here (finding B1).
"""

from __future__ import annotations

import pytest

# (method, path) for the routes that must never answer anonymously once an
# API key is set: PDAX money/account routes + the Stellar server-signed routes.
SECURED_ROUTES: list[tuple[str, str]] = [
    ("get", "/api/pdax/health/deep"),
    ("get", "/api/pdax/balances"),
    ("get", "/api/pdax/ramp"),
    ("get", "/api/pdax/fiat/transactions"),
    ("get", "/api/pdax/crypto/transactions"),
    ("post", "/api/pdax/fiat/withdraw"),
    ("post", "/api/pdax/crypto/withdraw"),
    ("post", "/api/pdax/fiat/deposit"),
    ("get", "/api/pdax/crypto/deposit"),
    ("post", "/api/pdax/trade/order"),
    ("post", "/api/pdax/trade/quote"),
    ("post", "/api/pdax/ramp/onramp"),
    ("post", "/api/pdax/ramp/offramp"),
    ("post", "/api/pdax/ramp/estimate"),
    ("post", "/api/pdax/webhooks/register"),
    ("post", "/api/stellar/server/charge"),
    ("post", "/api/stellar/server/seal"),
]


@pytest.mark.parametrize(("method", "path"), SECURED_ROUTES, ids=[f"{m.upper()} {p}" for m, p in SECURED_ROUTES])
def test_secured_route_requires_api_key(client, hermetic_settings, method: str, path: str) -> None:
    hermetic_settings.api_key = "secret-key"
    kwargs = {"json": {}} if method == "post" else {}
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401, f"{method.upper()} {path} answered {resp.status_code}, expected 401 without the key"
    assert resp.json()["detail"] == "invalid_api_key"


def test_secured_route_passes_the_guard_with_the_key(client, hermetic_settings) -> None:
    # With the key the request clears the guard and fails later (on unset creds),
    # never with the 401 the guard raises — proving the guard, not a coincidence.
    hermetic_settings.api_key = "secret-key"
    resp = client.get("/api/pdax/balances", headers={"X-API-Key": "secret-key"})
    assert resp.status_code != 401
