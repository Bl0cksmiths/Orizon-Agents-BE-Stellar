"""The execute authorization guard's reads and ownership check (story 5.01, seam audit S2, ADR 0011).

Every read is faked at the client seam (`sc.simulate_read`), so each outcome
the escrow can answer is driven exactly, and nothing reaches testnet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from escrow_fakes import AUTH, ESCROW, NOW, OTHER, PAYER, FakeEscrow, install, record, use_escrow

from app.services import authorization_guard as guard
from app.stellar import client as sc


@pytest.fixture(autouse=True)
def escrow_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    use_escrow(monkeypatch, ESCROW)
    monkeypatch.setattr(guard, "_wall_clock", lambda: NOW)
    guard.forget_versions()
    yield
    guard.forget_versions()


def refusal(coro: Any) -> guard.AuthorizationRefused:
    with pytest.raises(guard.AuthorizationRefused) as exc:
        asyncio.run(coro)
    return exc.value


def owned(auth_id: str = AUTH, payer: str = PAYER, plan_id: str = "pln_0a1b2c3d") -> Any:
    return guard.verify_ownership(auth_id, payer, plan_id)


def test_a_matching_v2_authorization_is_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow())
    verified = asyncio.run(owned(AUTH.upper()))
    assert verified.enforced and verified.escrow_version == 2
    assert verified.auth_id_hex == AUTH
    assert verified.authorization is not None and verified.authorization.label == "pln_0a1b2c3d"
    assert escrow.calls == [(ESCROW, "version"), (ESCROW, "authorization")]


@pytest.mark.parametrize(
    ("auth", "payer", "plan_id", "status", "code"),
    [
        (record(), OTHER, "pln_0a1b2c3d", 403, "authorization_payer_mismatch"),
        (record(agent_id="pln_ffffffff"), PAYER, "pln_0a1b2c3d", 409, "authorization_plan_mismatch"),
        (record(agent_id="orizon_batch"), PAYER, "pln_0a1b2c3d", 409, "authorization_plan_mismatch"),
        (record(settled=True, spent=420_000), PAYER, "pln_0a1b2c3d", 409, "authorization_spent"),
        (record(revoked=True), PAYER, "pln_0a1b2c3d", 409, "authorization_spent"),
    ],
)
def test_ownership_refusals_use_the_settle_lanes_codes(
    monkeypatch: pytest.MonkeyPatch, auth: dict[str, Any], payer: str, plan_id: str, status: int, code: str
) -> None:
    install(monkeypatch, FakeEscrow(auth=auth))
    err = refusal(owned(payer=payer, plan_id=plan_id))
    assert (err.status_code, err.detail, err.code) == (status, code, code)


def test_an_attacker_plan_against_a_victims_authorization_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """S2 itself: the victim's public (auth_id, payer), the attacker's own plan."""
    install(monkeypatch, FakeEscrow(auth=record(agent_id="pln_71c7132a")))
    assert refusal(owned(plan_id="pln_a77ac4e2")).code == "authorization_plan_mismatch"


def test_ownership_ignores_cap_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Those are the run's check (`execute_plan`); a release needs neither."""
    install(monkeypatch, FakeEscrow(auth=record(max_amount=1, expires_at=int(NOW) - 10)))
    assert asyncio.run(owned()).enforced


def test_a_missing_authorization_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEscrow(auth=RuntimeError("simulate failed: HostError: Error(Contract, #2)\nmore")))
    err = refusal(owned())
    assert (err.status_code, err.code) == (404, "authorization_not_found")


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("simulate failed: HostError: Error(Budget, ExceededLimit)"),
        ConnectionError("rpc down"),
        TimeoutError(),
    ],
)
def test_an_unreadable_version_is_503_never_a_pass(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    install(monkeypatch, FakeEscrow(version=failure))
    err = refusal(guard.escrow_version())
    assert (err.status_code, err.code) == (503, "authorization_unreadable")
    assert refusal(owned()).code == "authorization_unreadable"
    assert guard._versions == {}  # "could not read" is never remembered as an answer


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("simulate failed: HostError: Error(Contract, #1)"),
        ConnectionError("rpc down"),
        TimeoutError(),
    ],
)
def test_an_unreadable_authorization_is_503_never_a_pass(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    install(monkeypatch, FakeEscrow(auth=failure))
    err = refusal(owned())
    assert (err.status_code, err.code) == (503, "authorization_unreadable")


@pytest.mark.parametrize(
    "shape",
    [
        "not a record",
        {k: v for k, v in record().items() if k != "settled"},  # v1-shaped: no `settled`
        record(settled="false"),
        record(max_amount=True),
        record(max_amount="420000"),
        record(payer=None),
    ],
)
def test_a_malformed_authorization_is_503_never_coerced(monkeypatch: pytest.MonkeyPatch, shape: Any) -> None:
    install(monkeypatch, FakeEscrow(auth=shape))
    assert refusal(owned()).code == "authorization_unreadable"


@pytest.mark.parametrize("value", [0, -2, True, "2", None])
def test_a_nonsense_version_is_503(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    monkeypatch.setattr(sc, "simulate_read", lambda *a, **k: value)
    assert refusal(guard.escrow_version()).code == "authorization_unreadable"


def test_an_unknown_future_version_is_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEscrow(version=3))
    assert asyncio.run(guard.escrow_version()) == 3
    assert refusal(owned()).code == "authorization_unreadable"


def test_no_configured_escrow_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    use_escrow(monkeypatch, "")
    escrow = install(monkeypatch, FakeEscrow())
    assert refusal(guard.escrow_version()).code == "authorization_unreadable"
    assert escrow.calls == []


def test_v1_is_never_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 has no `version()`: nothing is read past that, and nothing is refused, whatever the input."""
    escrow = install(monkeypatch, FakeEscrow(version=None, auth=RuntimeError("never read")))
    verified = asyncio.run(owned(payer=OTHER, plan_id="pln_ffffffff"))
    assert not verified.enforced and verified.escrow_version == 1 and verified.authorization is None
    assert escrow.calls == [(ESCROW, "version")]


def test_a_definite_version_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=None))
    asyncio.run(guard.escrow_version())
    asyncio.run(guard.escrow_version())
    assert escrow.calls == [(ESCROW, "version")]


class SharedVersion:
    """The settle lane's `sc.escrow_version`, whose cache `execute_plan` also reads."""

    def __init__(self, answer: int | Exception) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def __call__(self, contract_id: str) -> int:
        self.calls.append(contract_id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_the_clients_shared_version_read_is_used_when_it_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = SharedVersion(2)
    monkeypatch.setattr(sc, "escrow_version", shared, raising=False)
    escrow = install(monkeypatch, FakeEscrow(version=RuntimeError("the fallback must not run")))
    assert asyncio.run(guard.escrow_version()) == 2
    assert shared.calls == [ESCROW] and escrow.calls == []


@pytest.mark.parametrize("answer", [RuntimeError("simulate failed: rpc"), 0, True])
def test_the_shared_version_read_is_503_when_unreadable(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    monkeypatch.setattr(sc, "escrow_version", SharedVersion(answer), raising=False)
    assert refusal(guard.escrow_version()).code == "authorization_unreadable"
