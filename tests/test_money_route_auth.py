"""Every money-moving / account-revealing route stays behind the API key.

The secured PDAX routes and the Stellar server-signed routes inherit
`require_api_key` from a shared sub-router, which is correct but easy to break
silently: a future refactor that moves one route back onto the public router
would pass every other test. This pins the invariant directly — with an API key
configured, each of these routes must answer 401 to an unauthenticated call —
so such a regression fails loudly here (finding B1).

The buyer's four dispute routes (story 4.02) are deliberately NOT on that list,
and `UNSECURED_BY_DESIGN` below says so in code rather than by omission. A file
that enumerates only the secured routes cannot tell "public on purpose" from
"forgotten to add" — which is the very confusion it exists to prevent — so the
public ones are listed too, with the reason, and asserted to stay public.

Two things make them public. They move no money: 4.02 records a claim, and
4.03's adjudication is what pays a credit. And the credential that guards the
write is the *payer's wallet signature*, which is the one thing a shared
operator key cannot express — worse, the operator holds that key, and the
operator is the party a dispute is raised against. One test below pins that:
with an API key configured and none supplied, the write reaches the verifier
and is stopped by the signature, not by the key.

`ADJUDICATION_ROUTES` (story 4.03) is the third list, and it exists because
neither of the first two describes it. Those routes are guarded, so they are
not public; but they are guarded by `require_adjudicator`, which FAILS CLOSED
where `require_api_key` waves an unset key through — so they cannot be folded
into `SECURED_ROUTES` either, whose test would read their 503 as a missing
guard. They are the routes that spend the platform's own balance, so the two
tests here pin both halves of the refusal: no key is a refusal, and the master
switch being off is a refusal, independently of each other.
"""

from __future__ import annotations

import base64

import pytest

from app.config import settings
from app.services import dispute_svc

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


def test_the_dispute_write_is_guarded_by_the_signature(client, hermetic_settings, monkeypatch) -> None:
    """The write is not unguarded — it is guarded by something else.

    With an API key configured and none supplied, the request must reach the
    signature verifier (proving the operator key is not what admits it) and be
    refused by that verifier's verdict (proving something still does). Both
    halves matter: the first alone would describe an open door, the second
    alone could be any error on the way.
    """
    hermetic_settings.api_key = "secret-key"
    signature = base64.b64encode(b"s" * 64).decode("ascii")
    verified: list[str] = []

    async def _open(**kwargs: object) -> None:
        verified.append(str(kwargs["signature_b64"]))
        # The rules lane's refusal for "you are not the wallet that paid",
        # built by attribute: the frozen contract fixes DisputeError's
        # attributes, not its constructor.
        exc = dispute_svc.DisputeError.__new__(dispute_svc.DisputeError)
        Exception.__init__(exc, "not_the_payer")
        exc.code = "not_the_payer"
        exc.message = "not the payer"
        exc.status_code = 403
        exc.existing = None
        raise exc

    monkeypatch.setattr(dispute_svc, "open_dispute", _open)

    resp = client.post(
        "/api/disputes",
        json={
            "job_id_hex": "1234567890abcdef1234567890abcdef",
            "step_index": 0,
            "reason": "the step delivered nothing",
            "payer": "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
            "nonce": "0123456789abcdef0123456789abcdef",
            "signature_b64": signature,
        },
    )

    assert verified == [signature], "the API key stopped the request before the signature was ever checked"
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "not_the_payer"


# (method, path) for story 4.03's adjudication pair: the routes that credit a
# dispute out of the platform's own settler balance, or close it for good. The
# highest-value writes in the API, and the only ones whose guard refuses rather
# than falls through when the operator has configured nothing.
ADJUDICATION_ROUTES: list[tuple[str, str]] = [
    ("post", "/api/disputes/dsp_0000000000000000/uphold"),
    ("post", "/api/disputes/dsp_0000000000000000/reject"),
]


def test_the_adjudication_routes_are_not_claimed_to_be_public() -> None:
    """The three inventories above must stay disjoint.

    Cheap, and it catches the one edit that would quietly undo this file: a
    4.03 route pasted into `UNSECURED_BY_DESIGN` — whose test asserts only
    "not 401" — would go on passing while the route that pays out answered
    anyone. Path-only, because a method is not what makes a route public.
    """
    adjudication = {path for _, path in ADJUDICATION_ROUTES}
    assert adjudication.isdisjoint(path for _, path in UNSECURED_BY_DESIGN)
    assert adjudication.isdisjoint(path for _, path in SECURED_ROUTES)


@pytest.mark.parametrize(
    ("method", "path"),
    ADJUDICATION_ROUTES,
    ids=[f"{m.upper()} {p}" for m, p in ADJUDICATION_ROUTES],
)
def test_adjudication_route_requires_the_api_key(
    client, hermetic_settings, monkeypatch, method: str, path: str
) -> None:
    # The switch is ON and the key IS configured, so the only thing missing is
    # the caller's credential — which is what makes the 401 mean the guard and
    # not the deployment's state.
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    hermetic_settings.api_key = "secret-key"

    resp = getattr(client, method)(path, json={})

    assert resp.status_code == 401, f"{method.upper()} {path} answered {resp.status_code}, expected 401 without the key"
    assert resp.json()["detail"] == "invalid_api_key"


@pytest.mark.parametrize(
    ("method", "path"),
    ADJUDICATION_ROUTES,
    ids=[f"{m.upper()} {p}" for m, p in ADJUDICATION_ROUTES],
)
def test_adjudication_route_is_refused_while_the_refund_switch_is_off(
    client, hermetic_settings, monkeypatch, method: str, path: str
) -> None:
    # The mirror image: the RIGHT key is supplied, so a refusal can only be the
    # master switch. A deployment that has not turned the refund path on must
    # not be able to pay a dispute out or dispose of one, whoever is asking.
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    hermetic_settings.api_key = "secret-key"

    resp = getattr(client, method)(path, json={}, headers={"X-API-Key": "secret-key"})

    assert resp.status_code == 503, f"{method.upper()} {path} answered {resp.status_code} with DISPUTE_REFUNDS off"
    assert resp.json()["error"]["code"] == "dispute_refunds_disabled"


# ── the one answer that precedes the guard ──────────────────────


def test_an_undecodable_body_is_answered_before_the_guard_and_tells_nobody_anything(
    client, hermetic_settings, monkeypatch
) -> None:
    """Pinned as a DECISION, not discovered as a bug.

    FastAPI decodes a request body before it solves dependencies, so a body
    that is not JSON at all reaches a 422 ahead of `require_adjudicator` and
    an anonymous caller sees 422 where they would otherwise see 503. Left as
    it is, because the 422 discloses strictly less than the guarded answer
    beside it — asserted here rather than argued:

      * a well-formed anonymous POST already answers 503, and a path that does
        not exist answers 404, so the route's existence is public either way;
      * a well-formed body with no `note` is 503 as well, so the model is
        validated after the guard like everything else and the 422 says only
        "this endpoint parses JSON";
      * nothing runs on any of these paths — no store read, no signature, no
        money.

    Closing it would mean taking the body as a raw `Request` and parsing it by
    hand, losing the declared model that makes the second bullet true and the
    request schema in the published spec. `routers/disputes.reject_dispute`
    carries the whole argument. If this test starts failing because the route
    now answers 401/503, that is an improvement and this test should go — it
    exists to stop the behaviour being *mistaken for an oversight*, not to
    keep it.
    """
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    hermetic_settings.api_key = "operator-secret-key"
    # `/reject` alone: it is the only one of the pair that declares a body, so
    # it is the only one with a decode to happen before the dependency.
    reject = "/api/disputes/dsp_0000000000000000/reject"

    undecodable = client.post(reject, content="{not json", headers={"content-type": "application/json"})
    well_formed = client.post(reject, json={"note": "a note"})
    no_note = client.post(reject, json={})
    absent_route = client.post("/api/disputes/dsp_0000000000000000/revoke", json={"note": "a note"})
    # `/uphold` takes no body at all, so the same request never reaches a
    # decode and is refused by the guard — the contrast that shows this is
    # about the declared model and not about the pair.
    uphold_undecodable = client.post(
        "/api/disputes/dsp_0000000000000000/uphold",
        content="{not json",
        headers={"content-type": "application/json"},
    )

    assert undecodable.status_code == 422
    assert uphold_undecodable.status_code == 503
    assert undecodable.json()["error"]["code"] == "validation_error"
    # What the 422 would supposedly reveal, revealed anyway by the guard.
    assert well_formed.status_code == 503
    assert well_formed.json()["error"]["code"] == "dispute_refunds_disabled"
    # And the schema is NOT revealed: a decodable body with the field missing
    # is refused by the guard, not by the model.
    assert no_note.status_code == 503
    # A route that does not exist still answers 404, so nothing above is the
    # only way to tell a real path from a made-up one.
    assert absent_route.status_code == 404
