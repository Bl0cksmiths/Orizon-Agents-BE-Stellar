"""Route-level tests for operator endpoint binding (story 2.01).

These sit above tests/test_external_binding_*.py, which cover the service in
isolation. What is tested HERE and nowhere else is the handler *ordering* —
the acceptance criteria are statements about what has and has not happened by
the time a request is refused, and only the route can demonstrate that:

  * AC-2: a rejected bind stores nothing;
  * AC-4: a blocked URL is refused without any outbound request;
  * and the two abuse properties the ordering exists for — a failed owner
    lookup must not burn the operator's nonce, and an anonymous caller must
    not be able to drive a chain read or a DNS resolution.

Every agent id below is unique to its test: the challenge table and the
binding store are process-global, exactly as they are in production, and
unique ids give isolation without reaching into module privates.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from stellar_sdk import Keypair

from app.services import external_binding
from app.services.binding_store import InMemoryBindingStore
from app.services.external_binding import OwnerLookupError

ENDPOINT = "https://operator.example/run"
OTHER_ENDPOINT = "https://other.example/run"


@pytest.fixture()
def store(monkeypatch):
    """A fresh binding store per test, injected at the router's seam so the
    tests never depend on the resolver's internal singleton."""
    fresh = InMemoryBindingStore()
    monkeypatch.setattr("app.routers.binding.get_binding_store", lambda: fresh)
    return fresh


@pytest.fixture()
def no_dns(monkeypatch):
    """Stub the bind-time resolution and record it. The suite is hermetic, and
    several tests assert this was NOT reached."""
    calls: list[str] = []

    async def _fake(url: str) -> tuple[str, ...]:
        calls.append(url)
        return ("93.184.216.34",)

    monkeypatch.setattr("app.routers.binding.resolve_and_check", _fake)
    return calls


def owned_by(monkeypatch, owner: str | None, *, fail: bool = False) -> list[str]:
    """Point the router's owner lookup at a known answer; record every call."""
    calls: list[str] = []

    async def _fake(agent_id: str) -> str | None:
        calls.append(agent_id)
        if fail:
            raise OwnerLookupError("rpc down")
        return owner

    monkeypatch.setattr(external_binding, "resolve_owner", _fake)
    return calls


def sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def challenge(client, agent_id: str, endpoint: str = ENDPOINT):
    return client.post(f"/api/agents/{agent_id}/bind/challenge", json={"endpoint_url": endpoint})


def stored(store, agent_id: str):
    """Read the store through its own (async) interface — no test-only hooks,
    so these assertions hold for the Postgres implementation too."""
    return asyncio.run(store.get(agent_id))


# ── the advisory preflight ──────────────────────────────────────


def test_endpoint_check_allows_a_public_https_url(client):
    r = client.get("/api/agents/bind/endpoint-check", params={"url": ENDPOINT})
    assert r.status_code == 200
    assert r.json() == {"allowed": True, "rule": None, "message": None}


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("http://operator.example/run", "scheme_not_https"),
        ("https://127.0.0.1/run", "non_public_address"),
        ("https://2130706433/run", "non_public_address"),  # the same address, spelled decimal
        ("https://localhost/run", "loopback_host"),
        ("https://metadata.google.internal/x", "metadata_host"),
    ],
)
def test_endpoint_check_names_the_rule(client, url: str, rule: str):
    # AC-4 says the refusal must name the rule. A machine-readable `rule` is
    # what makes that assertable here instead of regexing prose.
    r = client.get("/api/agents/bind/endpoint-check", params={"url": url})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is False
    assert body["rule"] == rule
    assert body["message"]


def test_endpoint_check_is_not_captured_as_an_agent_id(client):
    # GET /agents/{agent_id} has no pattern constraint, so a two-segment
    # spelling would be swallowed by it. This pins the path shape.
    r = client.get("/api/agents/bind/endpoint-check", params={"url": ENDPOINT})
    assert r.status_code == 200


# ── challenge ───────────────────────────────────────────────────


def test_challenge_returns_the_exact_message_to_sign(client, monkeypatch):
    owned_by(monkeypatch, "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV")
    r = challenge(client, "bind_msg1")
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == f"orizon-bind:v1:bind_msg1:{ENDPOINT}:{body['nonce']}"
    assert body["ttl_seconds"] == external_binding.CHALLENGE_TTL_SECONDS
    assert body["expires_at"] > 0


def test_challenge_refuses_an_unregistered_agent(client, monkeypatch):
    # Refusing before minting is what keeps the bounded challenge table from
    # being filled with ids that do not exist.
    owned_by(monkeypatch, None)
    r = challenge(client, "bind_ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "agent_not_found"


def test_challenge_fails_closed_when_the_chain_is_unreadable(client, monkeypatch):
    owned_by(monkeypatch, None, fail=True)
    r = challenge(client, "bind_rpcdown")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "registry_unavailable"


def test_challenge_refuses_a_blocked_endpoint_without_reading_the_chain(client, monkeypatch):
    calls = owned_by(monkeypatch, "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV")
    r = challenge(client, "bind_blocked1", "https://169.254.169.254/latest/meta-data/")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "endpoint_not_allowed"
    assert calls == []  # policy runs first — no Soroban amplification


def test_challenge_is_idempotent_within_its_window(client, monkeypatch):
    # Otherwise any anonymous caller can loop this and permanently stop the
    # real owner from completing a bind.
    owned_by(monkeypatch, "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV")
    first = challenge(client, "bind_idem1").json()
    second = challenge(client, "bind_idem1").json()
    assert first["nonce"] == second["nonce"]


# ── bind ────────────────────────────────────────────────────────


def test_bind_stores_the_endpoint_for_the_on_chain_owner(client, monkeypatch, store, no_dns):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_ok1").json()["message"]

    r = client.post(
        "/api/agents/bind_ok1/bind",
        json={"endpoint_url": ENDPOINT, "signature": sign(kp, message)},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["endpoint_url"] == ENDPOINT
    assert body["owner"] == kp.public_key
    assert body["replaced"] is False
    assert no_dns == [ENDPOINT]  # resolve-and-check ran, after authorisation


def test_bind_accepts_a_sep53_signature_end_to_end(client, monkeypatch, store, no_dns):
    # The path a REAL wallet takes. Every other bind test here signs the raw
    # UTF-8 bytes, but the frontend calls kit.signMessage(), and Freighter
    # implements SEP-53 — sign(sha256(b"Stellar Signed Message:\n" + msg)).
    # Without this test the whole feature could pass CI and still fail against
    # the most common Stellar wallet, and fail as "not_agent_owner" — looking
    # like a rejected owner rather than a signature-framing mismatch.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_sep53").json()["message"]

    r = client.post(
        "/api/agents/bind_sep53/bind",
        json={
            "endpoint_url": ENDPOINT,
            "signature": base64.b64encode(kp.sign_message(message.encode("utf-8"))).decode("ascii"),
        },
    )

    assert r.status_code == 200
    assert r.json()["owner"] == kp.public_key
    assert stored(store, "bind_sep53") is not None


def test_bind_refuses_a_signature_from_another_wallet(client, monkeypatch, store, no_dns):
    owner = Keypair.random()
    impostor = Keypair.random()
    owned_by(monkeypatch, owner.public_key)
    message = challenge(client, "bind_imp1").json()["message"]

    r = client.post(
        "/api/agents/bind_imp1/bind",
        json={"endpoint_url": ENDPOINT, "signature": sign(impostor, message)},
    )

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "not_agent_owner"
    assert stored(store, "bind_imp1") is None  # AC-2: nothing was stored


def test_bind_refuses_a_signature_bound_to_a_different_endpoint(client, monkeypatch, store, no_dns):
    # The replay the 1.06 prototype allowed: a signature captured for one URL
    # must not bind another inside the nonce's 5-minute window.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    issued = challenge(client, "bind_swap1").json()
    captured = sign(kp, issued["message"])

    r = client.post(
        "/api/agents/bind_swap1/bind",
        json={"endpoint_url": OTHER_ENDPOINT, "signature": captured},
    )

    assert r.status_code == 401
    assert stored(store, "bind_swap1") is None


def test_bind_refuses_a_replayed_nonce(client, monkeypatch, store, no_dns):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_replay1").json()["message"]
    payload = {"endpoint_url": ENDPOINT, "signature": sign(kp, message)}

    assert client.post("/api/agents/bind_replay1/bind", json=payload).status_code == 200
    again = client.post("/api/agents/bind_replay1/bind", json=payload)

    assert again.status_code == 401  # single use — a proven nonce never verifies twice


@pytest.mark.parametrize("signature", ["not base64!!", "c2hvcnQ=", "A" * 200])
def test_bind_refuses_a_malformed_signature(client, monkeypatch, store, no_dns, signature: str):
    calls = owned_by(monkeypatch, Keypair.random().public_key)
    r = client.post("/api/agents/bind_malf1/bind", json={"endpoint_url": ENDPOINT, "signature": signature})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "signature_malformed"
    assert calls == []  # a pure decode, settled before any chain read


def test_bind_refuses_a_blocked_endpoint_before_touching_anything(client, monkeypatch, store, no_dns):
    # AC-4, stated as an ordering property: no chain read, no DNS, no write.
    calls = owned_by(monkeypatch, Keypair.random().public_key)
    r = client.post(
        "/api/agents/bind_blocked2/bind",
        json={"endpoint_url": "https://10.0.0.5/run", "signature": base64.b64encode(b"x" * 64).decode()},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "endpoint_not_allowed"
    assert calls == []
    assert no_dns == []
    assert stored(store, "bind_blocked2") is None


def test_a_failed_owner_lookup_does_not_burn_the_nonce(client, monkeypatch, store, no_dns):
    # Otherwise an RPC outage becomes a griefing tool: anyone could consume
    # every pending operator's challenge just by calling bind during it.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_grief1").json()["message"]
    payload = {"endpoint_url": ENDPOINT, "signature": sign(kp, message)}

    owned_by(monkeypatch, None, fail=True)
    assert client.post("/api/agents/bind_grief1/bind", json=payload).status_code == 503

    owned_by(monkeypatch, kp.public_key)  # chain recovers
    assert client.post("/api/agents/bind_grief1/bind", json=payload).status_code == 200


def test_bind_fails_closed_when_the_chain_is_unreadable(client, monkeypatch, store, no_dns):
    owned_by(monkeypatch, None, fail=True)
    r = client.post(
        "/api/agents/bind_rpc2/bind",
        json={"endpoint_url": ENDPOINT, "signature": base64.b64encode(b"x" * 64).decode()},
    )
    # Not a 404: "chain unreachable" and "no such agent" are different facts,
    # and only one of them is retryable.
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "registry_unavailable"
    assert stored(store, "bind_rpc2") is None


# ── rebinding (AC-6) ────────────────────────────────────────────


def test_rebinding_replaces_the_endpoint_and_records_the_previous(client, monkeypatch, store, no_dns):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)

    first = challenge(client, "bind_re1").json()
    client.post("/api/agents/bind_re1/bind", json={"endpoint_url": ENDPOINT, "signature": sign(kp, first["message"])})

    second = challenge(client, "bind_re1", OTHER_ENDPOINT).json()
    r = client.post(
        "/api/agents/bind_re1/bind",
        json={"endpoint_url": OTHER_ENDPOINT, "signature": sign(kp, second["message"])},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["endpoint_url"] == OTHER_ENDPOINT
    assert body["replaced"] is True
    assert body["bound_at"] >= first["expires_at"] - external_binding.CHALLENGE_TTL_SECONDS


def test_a_rebind_requires_a_fresh_challenge(client, monkeypatch, store, no_dns):
    # There are no sessions here, so there is no "already bound, trust them"
    # shortcut: the second bind proves ownership again from scratch.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    issued = challenge(client, "bind_re2").json()
    client.post("/api/agents/bind_re2/bind", json={"endpoint_url": ENDPOINT, "signature": sign(kp, issued["message"])})

    r = client.post(
        "/api/agents/bind_re2/bind",
        json={"endpoint_url": OTHER_ENDPOINT, "signature": sign(kp, issued["message"])},
    )
    assert r.status_code == 401


# ── reading a binding back ──────────────────────────────────────


def test_reading_an_unbound_agent_is_a_404(client, store):
    r = client.get("/api/agents/bind_none1/binding")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "binding_not_found"


def test_anonymous_readers_get_the_host_only(client, monkeypatch, store, no_dns):
    # Publishing the full path would invite traffic that bypasses the
    # orchestrator entirely.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_read1").json()["message"]
    client.post("/api/agents/bind_read1/bind", json={"endpoint_url": ENDPOINT, "signature": sign(kp, message)})

    body = client.get("/api/agents/bind_read1/binding").json()
    assert body["endpoint_url"] == "https://operator.example"
    assert "/run" not in body["endpoint_url"]


def test_the_operator_key_discloses_the_full_url(client, monkeypatch, store, no_dns, hermetic_settings):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_read2").json()["message"]
    client.post("/api/agents/bind_read2/bind", json={"endpoint_url": ENDPOINT, "signature": sign(kp, message)})

    hermetic_settings.api_key = "operator-secret"
    body = client.get("/api/agents/bind_read2/binding", headers={"X-API-Key": "operator-secret"}).json()
    assert body["endpoint_url"] == ENDPOINT


def test_a_wrong_operator_key_still_gets_the_host_only(client, monkeypatch, store, no_dns, hermetic_settings):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    message = challenge(client, "bind_read3").json()["message"]
    client.post("/api/agents/bind_read3/bind", json={"endpoint_url": ENDPOINT, "signature": sign(kp, message)})

    hermetic_settings.api_key = "operator-secret"
    body = client.get("/api/agents/bind_read3/binding", headers={"X-API-Key": "wrong"}).json()
    assert body["endpoint_url"] == "https://operator.example"


def test_a_bad_agent_id_never_reaches_a_handler(client):
    # The Symbol charset is enforced at the router edge; `-` is not in it.
    assert client.get("/api/agents/not-a-symbol/binding").status_code == 422
