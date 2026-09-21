"""Every money-moving / account-revealing route stays behind the API key.

The secured PDAX routes and the Stellar server-signed routes inherit
`require_api_key` from a shared sub-router, which is correct but easy to break
silently: a future refactor that moves one route back onto the public router
would pass every other test. This pins the invariant directly — with an API key
configured, each of these routes must answer 401 to an unauthenticated call —
so such a regression fails loudly here (finding B1).

The dispute routes (story 4.02) are deliberately NOT on that list, and
`UNSECURED_BY_DESIGN` below says so in code rather than by omission. A file
that enumerates only the secured routes cannot tell "public on purpose" from
"forgotten to add" — which is the very confusion it exists to prevent — so the
public ones are listed too, with the reason, and asserted to stay public.

Two things make them public. They move no money: 4.02 records a claim, and
4.03 is what pays a credit. And the credential that guards the write is the
*payer's wallet signature*, which is the one thing a shared operator key
cannot express — worse, the operator holds that key, and the operator is the
party a dispute is raised against. The last test in this file pins that: with
an API key configured and none supplied, the write reaches the verifier and is
stopped by the signature, not by the key.
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


# (method, path) for the routes that must KEEP answering anonymously with an
# API key configured: the 4.02 dispute surface. Listed rather than omitted, so
# a future refactor that quietly moves one behind the operator key — which
# would lock every buyer out of the window they were promised — fails here too.
UNSECURED_BY_DESIGN: list[tuple[str, str]] = [
    ("post", "/api/disputes/challenge"),
    ("post", "/api/disputes"),
    ("get", "/api/disputes/dsp_0000000000000000"),
    ("get", "/api/tasks/task-unknown/disputes"),
]


@pytest.mark.parametrize(
    ("method", "path"),
    UNSECURED_BY_DESIGN,
    ids=[f"{m.upper()} {p}" for m, p in UNSECURED_BY_DESIGN],
)
def test_dispute_route_stays_public_with_a_key_configured(client, hermetic_settings, method: str, path: str) -> None:
    hermetic_settings.api_key = "secret-key"
    kwargs = {"json": {}} if method == "post" else {}
    resp = getattr(client, method)(path, **kwargs)
    # Whatever else it answers — 422 for the empty bodies, 404 for the unknown
    # ids — it must never be the 401 the operator-key guard raises.
    assert resp.status_code != 401, f"{method.upper()} {path} is behind the API key; disputes are signature-authorized"
