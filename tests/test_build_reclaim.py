"""POST /api/stellar/build/reclaim (story 5.01, ADR 0011).

The contract is frozen — the frontend codes against it: body
`{payer, auth_id_hex}` (32 lowercase hex), answer `{xdr}`, an unsigned
prepared `reclaim(payer, auth_id)` with the payer as source. Every refusal the
contract would make is named before the wallet is asked to sign.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from escrow_fakes import AUTH, FakeEscrow, install, record, use_escrow
from fastapi.testclient import TestClient
from stellar_sdk import Account, Address, Keypair, StrKey, TransactionEnvelope, scval

from app.services import authorization_guard as guard
from app.stellar import client as sc

PAYER = Keypair.from_raw_ed25519_seed(bytes(range(32))).public_key
STRANGER = Keypair.from_raw_ed25519_seed(bytes(range(1, 33))).public_key
ESCROW = StrKey.encode_contract(bytes(range(32, 64)))


class BuildRecorder:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, list[Any], str]] = []
        self.fail = fail

    def __call__(self, contract_id: str, function_name: str, args: list[Any], source: str) -> str:
        self.calls.append((contract_id, function_name, args, source))
        if self.fail is not None:
            raise self.fail
        return "AAAA-unsigned-xdr"


@pytest.fixture(autouse=True)
def escrow(monkeypatch: pytest.MonkeyPatch) -> None:
    use_escrow(monkeypatch, ESCROW)
    guard.forget_versions()
    yield
    guard.forget_versions()


@pytest.fixture()
def builds(monkeypatch: pytest.MonkeyPatch) -> BuildRecorder:
    recorder = BuildRecorder()
    monkeypatch.setattr(sc, "build_invoke_xdr", recorder)
    return recorder


def expired(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"payer": PAYER, "expires_at": int(time.time()) - 60}
    fields.update(overrides)
    return record(**fields)


def reclaim(client: TestClient, payer: str = PAYER, auth: str = AUTH) -> Any:
    return client.post("/api/stellar/build/reclaim", json={"payer": payer, "auth_id_hex": auth})


def test_an_expired_authorization_builds_a_reclaim_for_its_payer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder
) -> None:
    install(monkeypatch, FakeEscrow(auth=expired()))
    r = reclaim(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"xdr": "AAAA-unsigned-xdr"}
    [(contract_id, function_name, args, source)] = builds.calls
    assert (contract_id, function_name, source) == (ESCROW, "reclaim", PAYER)
    assert [a.to_xdr() for a in args] == [sc.addr(PAYER).to_xdr(), sc.bytes16(bytes.fromhex(AUTH)).to_xdr()]


def test_the_xdr_is_an_unsigned_reclaim_with_the_payer_as_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real builder, down to the envelope the wallet will be handed."""
    install(monkeypatch, FakeEscrow(auth=expired()))

    class Server:
        def load_account(self, account_id: str) -> Account:
            return Account(account_id, 41)

        def prepare_transaction(self, tx: Any) -> Any:
            return tx

    monkeypatch.setattr(sc, "_server", lambda *, submit=False: Server())
    r = reclaim(client)
    assert r.status_code == 200, r.text
    env = TransactionEnvelope.from_xdr(r.json()["xdr"], sc.network_passphrase())
    assert env.signatures == []
    assert env.transaction.source.account_id == PAYER
    [op] = env.transaction.operations
    invoke = op.host_function.invoke_contract
    assert Address.from_xdr_sc_address(invoke.contract_address).address == ESCROW
    assert invoke.function_name.sc_symbol == b"reclaim"
    assert scval.to_native(invoke.args[0]).address == PAYER
    assert scval.to_native(invoke.args[1]) == bytes.fromhex(AUTH)


@pytest.mark.parametrize(
    ("auth", "payer", "status", "code"),
    [
        (expired(settled=True), PAYER, 409, "authorization_settled"),
        (expired(revoked=True), PAYER, 409, "authorization_revoked"),
        (expired(expires_at=int(time.time()) + 3_600), PAYER, 409, "authorization_locked"),
        (expired(), STRANGER, 403, "authorization_payer_mismatch"),
    ],
)
def test_every_refusal_is_named_before_anything_is_built(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    builds: BuildRecorder,
    auth: dict[str, Any],
    payer: str,
    status: int,
    code: str,
) -> None:
    install(monkeypatch, FakeEscrow(auth=auth))
    r = reclaim(client, payer=payer)
    assert r.status_code == status
    assert r.json()["detail"] == code and r.json()["error"]["code"] == code
    assert builds.calls == []


def test_settled_and_reclaimed_each_have_their_documented_code_and_say_which(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder
) -> None:
    # The route's docstring (and the OpenAPI the FE vendors) promise
    # `authorization_settled` and `authorization_revoked`, and the console's
    # reclaim flow maps exactly those two to "already settled" / "already
    # reclaimed". A shared `authorization_spent` matched neither, so a buyer
    # pressing Reclaim on a settled authorization was shown a failure instead.
    install(monkeypatch, FakeEscrow(auth=expired(settled=True)))
    settled = reclaim(client).json()["error"]
    assert settled["code"] == "authorization_settled"
    assert "settled" in settled["message"]
    install(monkeypatch, FakeEscrow(auth=expired(revoked=True)))
    revoked = reclaim(client).json()["error"]
    assert revoked["code"] == "authorization_revoked"
    assert "reclaimed" in revoked["message"]


def test_locked_holds_past_expiry_until_the_ledger_can_have_caught_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder
) -> None:
    now = 1_900_000_000.0
    monkeypatch.setattr(guard, "_wall_clock", lambda: now)
    install(monkeypatch, FakeEscrow(auth=expired(expires_at=int(now) - 5)))
    r = reclaim(client)
    assert r.json()["error"]["code"] == "authorization_locked"
    assert str(int(now) - 5) in r.json()["error"]["message"]
    install(monkeypatch, FakeEscrow(auth=expired(expires_at=int(now - guard.LEDGER_CLOCK_ALLOWANCE_SECONDS) - 1)))
    assert reclaim(client).status_code == 200


def test_a_missing_authorization_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder
) -> None:
    install(monkeypatch, FakeEscrow(auth=RuntimeError("simulate failed: HostError: Error(Contract, #2)")))
    r = reclaim(client)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "authorization_not_found")
    assert builds.calls == []


def test_v1_has_nothing_to_reclaim(client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=None, auth=RuntimeError("never read")))
    r = reclaim(client)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "reclaim_unsupported")
    assert builds.calls == [] and escrow.calls == [(ESCROW, "version")]


@pytest.mark.parametrize("where", ["version", "authorization"])
def test_an_unreadable_chain_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder, where: str
) -> None:
    down = ConnectionError("rpc down")
    install(monkeypatch, FakeEscrow(version=down) if where == "version" else FakeEscrow(auth=down))
    r = reclaim(client)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "authorization_unreadable")
    assert builds.calls == []


def test_a_build_that_fails_is_400_build_failed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEscrow(auth=expired()))
    monkeypatch.setattr(sc, "build_invoke_xdr", BuildRecorder(fail=RuntimeError("account not found")))
    r = reclaim(client)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "build_failed")


@pytest.mark.parametrize(
    "body",
    [
        {"payer": PAYER, "auth_id_hex": AUTH.upper()},  # lowercase only
        {"payer": PAYER, "auth_id_hex": AUTH[:-1]},
        {"payer": PAYER, "auth_id_hex": AUTH + "0"},
        {"payer": PAYER, "auth_id_hex": "zz" * 16},
        {"payer": PAYER.lower(), "auth_id_hex": AUTH},
        {"payer": "not-an-address", "auth_id_hex": AUTH},
        {"auth_id_hex": AUTH},
        {"payer": PAYER},
    ],
)
def test_the_body_is_validated_before_any_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder, body: dict[str, Any]
) -> None:
    escrow = install(monkeypatch, FakeEscrow(auth=expired()))
    r = client.post("/api/stellar/build/reclaim", json=body)
    assert r.status_code == 422
    assert escrow.calls == [] and builds.calls == []


def test_it_spends_the_service_rate_limit_like_its_siblings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, builds: BuildRecorder
) -> None:
    install(monkeypatch, FakeEscrow(auth=expired()))
    r = reclaim(client)
    assert r.headers.get("x-ratelimit-limit") is not None
    assert r.headers.get("x-ratelimit-remaining") is not None
