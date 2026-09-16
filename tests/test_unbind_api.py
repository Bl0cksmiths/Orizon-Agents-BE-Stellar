"""Route-level tests for `DELETE /api/agents/{id}/bind` — the operator's stop.

Before this route existed there was no way to stop work reaching a bound
endpoint. `AgentRegistry.set_active(id, false)` only flips the mirrored
marketplace status, the failure tracker only logs, and the in-memory routable
set only ever grew — so an attacker holding a compromised operator host kept
receiving signed dispatch envelopes carrying the buyer's intent, rationale and
accumulated context, indefinitely. That is what these tests are protecting.

tests/test_unbind_challenge.py covers the proof in isolation. What is tested
HERE and nowhere else is what the ROUTE does with it, which is a set of
statements about what has and has not happened by the time a request is answered:

  * the owner's revocation takes the agent out of the store AND out of the
    planner's synchronous routable set, in the same request;
  * a refused revocation changes NOTHING — not the store, not the set, not the
    honest owner's outstanding challenge;
  * an unreadable chain is a 503, never an accidental "permission granted";
  * every refusal answers the same `not_agent_owner`, so the route cannot be
    used to probe which agents exist, who owns them, or which ones are bound.

Every agent id below is unique to its test: the challenge table, the binding
store and the routable set are process-global, exactly as they are in
production, and unique ids give isolation without reaching into module privates.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from stellar_sdk import Keypair

from app.services import binding_registry, external_binding
from app.services.binding_store import InMemoryBindingStore
from app.services.external_binding import OwnerLookupError

ENDPOINT = "https://operator.example/run"


@pytest.fixture()
def store(monkeypatch):
    """A fresh binding store per test, injected at the router's seam so the
    tests never depend on the resolver's internal singleton."""
    fresh = InMemoryBindingStore()
    monkeypatch.setattr("app.routers.binding.get_binding_store", lambda: fresh)
    return fresh


@pytest.fixture(autouse=True)
def routable_set():
    """Restore the planner's routable set afterwards.

    The route mutates it for real — that is half of what is under test — and it
    is module state shared with every other suite, so it is snapshotted rather
    than left for the next test to inherit.
    """
    saved = set(binding_registry._bound_ids)
    yield binding_registry._bound_ids
    binding_registry._bound_ids.clear()
    binding_registry._bound_ids.update(saved)


@pytest.fixture()
def no_dns(monkeypatch):
    """Stub the bind-time resolution: the suite is hermetic, and these tests
    need a real bind in place before they can revoke it."""

    async def _fake(url: str) -> tuple[str, ...]:
        return ("93.184.216.34",)

    monkeypatch.setattr("app.routers.binding.resolve_and_check", _fake)


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


def stored(store, agent_id: str):
    """Read the store through its own (async) interface — no test-only hooks,
    so these assertions hold for the Postgres implementation too."""
    return asyncio.run(store.get(agent_id))


def bind(client, keypair: Keypair, agent_id: str) -> None:
    """Complete a real bind through the public routes, so what a revocation is
    revoking is a binding this service actually accepted."""
    issued = client.post(f"/api/agents/{agent_id}/bind/challenge", json={"endpoint_url": ENDPOINT})
    assert issued.status_code == 200
    r = client.post(
        f"/api/agents/{agent_id}/bind",
        json={"endpoint_url": ENDPOINT, "signature": sign(keypair, issued.json()["message"])},
    )
    assert r.status_code == 200


def unbind_challenge(client, agent_id: str):
    return client.post(f"/api/agents/{agent_id}/unbind/challenge")


def unbind(client, agent_id: str, signature: str):
    # httpx's `.delete()` takes no body, so the request is spelled out. A DELETE
    # with a body is what carries the proof; there is nowhere else to put a
    # signature that does not end up in an access log or a URL.
    return client.request("DELETE", f"/api/agents/{agent_id}/bind", json={"signature": signature})


def revoke(client, keypair: Keypair, agent_id: str):
    """Mint a challenge, sign it, and revoke — the whole operator flow."""
    issued = unbind_challenge(client, agent_id)
    assert issued.status_code == 200
    return unbind(client, agent_id, sign(keypair, issued.json()["message"]))


# ── the challenge ───────────────────────────────────────────────


def test_the_challenge_returns_the_exact_message_to_sign(client, monkeypatch):
    owned_by(monkeypatch, Keypair.random().public_key)

    body = unbind_challenge(client, "ub_msg1").json()

    assert body["message"] == f"orizon-unbind:v1:ub_msg1:{body['nonce']}"
    assert body["ttl_seconds"] == external_binding.CHALLENGE_TTL_SECONDS
    assert body["expires_at"] > 0


def test_the_challenge_refuses_an_unregistered_agent(client, monkeypatch):
    # Refusing before minting is what keeps the bounded challenge table from
    # being filled with ids that do not exist.
    owned_by(monkeypatch, None)

    r = unbind_challenge(client, "ub_ghost")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "agent_not_found"


def test_the_challenge_fails_closed_when_the_chain_is_unreadable(client, monkeypatch):
    owned_by(monkeypatch, None, fail=True)

    r = unbind_challenge(client, "ub_rpcdown")

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "registry_unavailable"


# ── the owner can stop dispatch ─────────────────────────────────


def test_the_owner_can_unbind_and_the_agent_stops_being_dispatchable(client, monkeypatch, store, no_dns):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_ok1")
    assert binding_registry.is_dispatchable("ub_ok1") is True

    r = revoke(client, kp, "ub_ok1")

    assert r.status_code == 200
    body = r.json()
    assert body == {
        "agent_id": "ub_ok1",
        "owner": kp.public_key,
        "was_bound": True,
        "unbound_at": body["unbound_at"],
    }
    assert body["unbound_at"] > 0
    # Both halves, in the same request. The store is what the dispatch path
    # reads; the set is what the planner filters on. A revocation that moved
    # only one of them would still be handing work to the revoked host.
    assert stored(store, "ub_ok1") is None
    assert binding_registry.is_dispatchable("ub_ok1") is False


def test_a_revoked_agent_reads_back_as_unbound(client, monkeypatch, store, no_dns):
    # `GET /{id}/binding` is the only way an operator can confirm the state,
    # so it has to agree with what the revocation claims.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_read1")
    assert client.get("/api/agents/ub_read1/binding").status_code == 200

    revoke(client, kp, "ub_read1")

    r = client.get("/api/agents/ub_read1/binding")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "binding_not_found"


def test_unbinding_twice_is_not_an_error(client, monkeypatch, store, no_dns):
    """The end state the caller asked for holds, so the request succeeded. A
    404 would report failure for a request that achieved exactly what it asked,
    and would push a client that cannot tell a lost response from a lost
    binding into retrying a revocation that already worked."""
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_twice1")

    assert revoke(client, kp, "ub_twice1").json()["was_bound"] is True
    second = revoke(client, kp, "ub_twice1")

    assert second.status_code == 200
    assert second.json()["was_bound"] is False
    # Honest about the difference: nothing was revoked, so no time is reported
    # for a revocation that did not happen.
    assert second.json()["unbound_at"] is None


def test_unbinding_an_agent_that_never_bound_is_a_200(client, monkeypatch, store):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)

    r = revoke(client, kp, "ub_never1")

    assert r.status_code == 200
    assert r.json()["was_bound"] is False


def test_a_rebind_after_a_revocation_works_and_replaces_nothing(client, monkeypatch, store, no_dns):
    # A revocation is not a ban. The operator moves to a clean host and binds
    # again, and `replaced` says the truth: there was nothing live to replace.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_again1")
    revoke(client, kp, "ub_again1")

    issued = client.post("/api/agents/ub_again1/bind/challenge", json={"endpoint_url": ENDPOINT})
    r = client.post(
        "/api/agents/ub_again1/bind",
        json={"endpoint_url": ENDPOINT, "signature": sign(kp, issued.json()["message"])},
    )

    assert r.status_code == 200
    assert r.json()["replaced"] is False
    assert binding_registry.is_dispatchable("ub_again1") is True


# ── nobody else can ─────────────────────────────────────────────


def test_a_non_owner_cannot_unbind_and_nothing_changes(client, monkeypatch, store, no_dns):
    kp = Keypair.random()
    impostor = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_imp1")

    issued = unbind_challenge(client, "ub_imp1").json()
    r = unbind(client, "ub_imp1", sign(impostor, issued["message"]))

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "not_agent_owner"
    # Nothing moved: the binding is intact and the agent is still routable.
    record = stored(store, "ub_imp1")
    assert record is not None and record.endpoint_url == ENDPOINT
    assert binding_registry.is_dispatchable("ub_imp1") is True
    # And the attempt did not burn the honest owner's challenge — otherwise
    # anyone could grief a revocation in progress by guessing at it.
    assert unbind(client, "ub_imp1", sign(kp, issued["message"])).status_code == 200


def test_a_captured_bind_signature_cannot_unbind(client, monkeypatch, store, no_dns):
    """The replay the two domain separators exist to stop. A bind signature is
    not a secret: it goes to a public route and may sit in a proxy log forever.
    If it could also revoke, anyone who ever watched an operator bind could take
    that agent out of the network."""
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    issued = client.post("/api/agents/ub_replay1/bind/challenge", json={"endpoint_url": ENDPOINT})
    captured = sign(kp, issued.json()["message"])
    assert (
        client.post(
            "/api/agents/ub_replay1/bind",
            json={"endpoint_url": ENDPOINT, "signature": captured},
        ).status_code
        == 200
    )

    # A live unbind challenge exists, so the only thing standing between the
    # captured signature and a revocation is the message it covers.
    unbind_challenge(client, "ub_replay1")
    r = unbind(client, "ub_replay1", captured)

    assert r.status_code == 401
    assert stored(store, "ub_replay1") is not None
    assert binding_registry.is_dispatchable("ub_replay1") is True


def test_an_unbind_without_a_challenge_is_refused(client, monkeypatch, store, no_dns):
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_nochal1")

    # A well-formed signature over the right message, but no nonce was ever
    # minted — so there is nothing that proves it is recent.
    forged = sign(kp, external_binding.unbinding_message("ub_nochal1", "deadbeef"))
    r = unbind(client, "ub_nochal1", forged)

    assert r.status_code == 401
    assert stored(store, "ub_nochal1") is not None


def test_a_replayed_unbind_signature_is_refused(client, monkeypatch, store, no_dns):
    # Single use. Otherwise a captured unbind could be held and replayed to
    # knock the agent out again after the operator rebound it.
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_reuse1")
    issued = unbind_challenge(client, "ub_reuse1").json()
    signature = sign(kp, issued["message"])

    assert unbind(client, "ub_reuse1", signature).status_code == 200
    assert unbind(client, "ub_reuse1", signature).status_code == 401


def test_every_refusal_answers_the_same_code(client, monkeypatch, store, no_dns):
    """The non-discriminating 401 `bind` already promises, restated for this
    route. Splitting "no challenge" from "wrong signer" from "not bound" would
    turn a public endpoint into an oracle for which agents exist, who owns them
    and which of them are live."""
    kp = Keypair.random()
    impostor = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_uniform1")
    bogus = base64.b64encode(b"x" * 64).decode("ascii")

    no_challenge = unbind(client, "ub_uniform1", bogus)
    issued = unbind_challenge(client, "ub_uniform1").json()
    wrong_signer = unbind(client, "ub_uniform1", sign(impostor, issued["message"]))
    # An agent that exists on chain but has no binding at all: still one 401,
    # so "is this agent bound?" cannot be answered by a failed revocation.
    never_bound = unbind(client, "ub_uniform2", bogus)

    for r in (no_challenge, wrong_signer, never_bound):
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "not_agent_owner"


# ── fail closed, and refuse cheaply ─────────────────────────────


def test_an_unreadable_chain_is_a_503_and_not_a_revocation(client, monkeypatch, store, no_dns):
    """`OwnerLookupError` must never become "permission granted" — but it must
    not become a revocation either. The binding survives an RPC outage."""
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    bind(client, kp, "ub_rpc1")
    issued = unbind_challenge(client, "ub_rpc1").json()

    owned_by(monkeypatch, None, fail=True)
    r = unbind(client, "ub_rpc1", sign(kp, issued["message"]))

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "registry_unavailable"
    assert stored(store, "ub_rpc1") is not None
    assert binding_registry.is_dispatchable("ub_rpc1") is True

    owned_by(monkeypatch, kp.public_key)  # chain recovers
    # The outage did not burn the challenge: the nonce is consumed only on a
    # proven signature, so an RPC failure cannot grief a pending revocation.
    assert unbind(client, "ub_rpc1", sign(kp, issued["message"])).status_code == 200


def test_an_agent_that_is_not_on_chain_is_a_404(client, monkeypatch, store):
    # "No such agent" and "chain unreachable" are different facts, and only one
    # of them is worth retrying.
    owned_by(monkeypatch, None)

    r = unbind(client, "ub_ghost2", base64.b64encode(b"x" * 64).decode("ascii"))

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "agent_not_found"


@pytest.mark.parametrize("signature", ["not base64!!", "c2hvcnQ=", "A" * 200])
def test_a_malformed_signature_is_settled_before_any_chain_read(client, monkeypatch, store, signature: str):
    calls = owned_by(monkeypatch, Keypair.random().public_key)

    r = unbind(client, "ub_malf1", signature)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "signature_malformed"
    assert calls == []  # a pure decode, so a client bug costs no RPC round trip


def test_a_bad_agent_id_never_reaches_a_handler(client):
    # The Symbol charset is enforced at the router edge; `-` is not in it.
    r = client.request("DELETE", "/api/agents/not-a-symbol/bind", json={"signature": "x"})
    assert r.status_code == 422
