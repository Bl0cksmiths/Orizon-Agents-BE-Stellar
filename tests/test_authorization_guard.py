"""The execute authorization guard's verification (story 5.01, seam audit S2, ADR 0011).

Every read is faked at the client seam (`sc.simulate_read`), so each outcome
the escrow can answer is driven exactly, and nothing reaches testnet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from escrow_fakes import AUTH, ESCROW, NOW, OTHER, PAYER, FakeEscrow, install, make_plan, record, use_escrow

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


def test_a_matching_v2_authorization_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow())
    verified = asyncio.run(guard.verify(AUTH.upper(), PAYER, make_plan()))
    assert verified.enforced and verified.escrow_version == 2
    assert verified.auth_id_hex == AUTH
    assert verified.authorization is not None and verified.authorization.label == "pln_0a1b2c3d"
    assert escrow.calls == [(ESCROW, "version"), (ESCROW, "authorization")]


@pytest.mark.parametrize(
    ("auth", "payer", "plan_id", "status", "code"),
    [
        (record(), OTHER, "pln_0a1b2c3d", 403, "authorization_payer_mismatch"),
        (record(agent_id="pln_ffffffff"), PAYER, "pln_0a1b2c3d", 403, "authorization_plan_mismatch"),
        (record(agent_id="orizon_batch"), PAYER, "pln_0a1b2c3d", 403, "authorization_plan_mismatch"),
        (record(settled=True, spent=420_000), PAYER, "pln_0a1b2c3d", 409, "authorization_settled"),
        (record(revoked=True), PAYER, "pln_0a1b2c3d", 409, "authorization_revoked"),
    ],
)
def test_ownership_refusals(
    monkeypatch: pytest.MonkeyPatch, auth: dict[str, Any], payer: str, plan_id: str, status: int, code: str
) -> None:
    install(monkeypatch, FakeEscrow(auth=auth))
    err = refusal(guard.verify(AUTH, payer, make_plan(plan_id)))
    assert (err.status_code, err.detail, err.code) == (status, code, code)


def test_an_attacker_plan_against_a_victims_authorization_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """S2 itself: the victim's public (auth_id, payer), the attacker's own plan."""
    install(monkeypatch, FakeEscrow(auth=record(agent_id="pln_71c7132a")))
    err = refusal(guard.verify(AUTH, PAYER, make_plan("pln_a77ac4e2")))
    assert err.code == "authorization_plan_mismatch"


def test_a_missing_authorization_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEscrow(auth=RuntimeError("simulate failed: HostError: Error(Contract, #2)\nmore")))
    err = refusal(guard.verify(AUTH, PAYER, make_plan()))
    assert (err.status_code, err.code) == (404, "authorization_not_found")


def test_the_cap_must_cover_the_per_step_payouts_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0.012 + 0.03 USDC = 120_000 + 300_000 stroops, with 0.012 * 1e7 carrying float noise.
    install(monkeypatch, FakeEscrow(auth=record(max_amount=420_000)))
    assert asyncio.run(guard.verify(AUTH, PAYER, make_plan())).enforced
    install(monkeypatch, FakeEscrow(auth=record(max_amount=419_999)))
    err = refusal(guard.verify(AUTH, PAYER, make_plan()))
    assert (err.status_code, err.code) == (409, "authorization_insufficient")


def test_a_cap_equal_to_a_rounded_down_total_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quoted total rounded to 4 dp (the old DecomposeResponse) is not enough."""
    plan = make_plan(prices=(0.01234, 0.01234))  # 0.02468 USDC = 246_800 stroops; 4 dp is 0.0247 up, 0.0246 down
    install(monkeypatch, FakeEscrow(auth=record(max_amount=sc.usdc_to_i128(0.0246))))
    assert refusal(guard.verify(AUTH, PAYER, plan)).code == "authorization_insufficient"
    install(monkeypatch, FakeEscrow(auth=record(max_amount=246_800)))
    assert asyncio.run(guard.verify(AUTH, PAYER, plan)).enforced


def test_a_fractional_stroop_total_rounds_up_not_down() -> None:
    plan = make_plan(prices=(0.1,))
    plan = plan.model_copy(update={"total_usdc": 0.12345675})  # 1_234_567.5 stroops
    assert guard.required_stroops(plan) == 1_234_568


def test_a_price_the_ledger_cannot_hold_is_never_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = make_plan().model_copy(update={"total_usdc": float("inf")})
    assert guard.required_stroops(plan) is None
    install(monkeypatch, FakeEscrow(auth=record(max_amount=10**18)))
    assert refusal(guard.verify(AUTH, PAYER, plan)).code == "authorization_insufficient"


@pytest.mark.parametrize("left", [-1, 0, 60])
def test_an_expired_or_expiring_authorization_is_refused(monkeypatch: pytest.MonkeyPatch, left: int) -> None:
    install(monkeypatch, FakeEscrow(auth=record(expires_at=int(NOW) + left)))
    err = refusal(guard.verify(AUTH, PAYER, make_plan()))
    assert (err.status_code, err.code) == (409, "authorization_expired")


def test_the_expiry_margin_is_a_worst_case_run(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = make_plan()
    margin = guard.run_margin_seconds(len(plan.plan.steps))
    assert margin > 2 * 120  # at least both step deadlines
    install(monkeypatch, FakeEscrow(auth=record(expires_at=int(NOW + margin) - 1)))
    assert refusal(guard.verify(AUTH, PAYER, plan)).code == "authorization_expired"
    install(monkeypatch, FakeEscrow(auth=record(expires_at=int(NOW + margin) + 1)))
    assert asyncio.run(guard.verify(AUTH, PAYER, plan)).enforced


def test_the_settle_lanes_worst_case_is_used_when_it_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import execution_svc

    monkeypatch.setattr(execution_svc, "worst_case_run_seconds", lambda n: 1000.0 + n, raising=False)
    assert guard.run_margin_seconds(3) == 1003.0


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
    err = refusal(guard.verify(AUTH, PAYER, make_plan()))
    assert (err.status_code, err.code) == (503, "authorization_unverifiable")
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
    err = refusal(guard.verify(AUTH, PAYER, make_plan()))
    assert (err.status_code, err.code) == (503, "authorization_unverifiable")


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
    assert refusal(guard.verify(AUTH, PAYER, make_plan())).code == "authorization_unverifiable"


@pytest.mark.parametrize("value", [0, -2, True, "2", None])
def test_a_nonsense_version_is_503(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    monkeypatch.setattr(sc, "simulate_read", lambda *a, **k: value)
    assert refusal(guard.verify(AUTH, PAYER, make_plan())).code == "authorization_unverifiable"


def test_an_unknown_future_version_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEscrow(version=3))
    assert refusal(guard.verify(AUTH, PAYER, make_plan())).code == "authorization_unverifiable"


def test_no_configured_escrow_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    use_escrow(monkeypatch, "")
    escrow = install(monkeypatch, FakeEscrow())
    assert refusal(guard.verify(AUTH, PAYER, make_plan())).code == "authorization_unverifiable"
    assert escrow.calls == []


def test_v1_is_unenforced_and_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """v1 has no `version()`: nothing is read past that, and nothing is refused, whatever the input."""
    escrow = install(monkeypatch, FakeEscrow(version=None, auth=RuntimeError("never read")))
    with caplog.at_level("WARNING", logger="app.services.authorization_guard"):
        verified = asyncio.run(guard.verify(AUTH, OTHER, make_plan("pln_ffffffff")))
    assert not verified.enforced and verified.escrow_version == 1 and verified.authorization is None
    assert escrow.calls == [(ESCROW, "version")]
    assert "v1 PaymentEscrow" in caplog.text and "NOT verified" in caplog.text


def test_a_definite_version_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=None))
    asyncio.run(guard.verify(AUTH, PAYER, make_plan()))
    asyncio.run(guard.verify(AUTH, PAYER, make_plan()))
    assert escrow.calls == [(ESCROW, "version")]


def test_verify_ownership_skips_the_plan_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan that no longer exists cannot be priced; ownership is all a release needs."""
    install(monkeypatch, FakeEscrow(auth=record(max_amount=1, expires_at=int(NOW) - 10)))
    verified = asyncio.run(guard.verify_ownership(AUTH, PAYER, "pln_0a1b2c3d"))
    assert verified.enforced
    assert refusal(guard.verify_ownership(AUTH, OTHER, "pln_0a1b2c3d")).code == "authorization_payer_mismatch"
    assert refusal(guard.verify_ownership(AUTH, PAYER, "pln_ffffffff")).code == "authorization_plan_mismatch"
