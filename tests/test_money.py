"""The one stroops helper (ADR 0015): exact conversion, one display rule, the real asset."""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal

import pytest

from app import money
from app.config import settings

TESTNET = "Test SDF Network ; September 2015"
# The testnet native SAC, as `Asset.native().contract_id(TESTNET)` derives it and
# as the escrow v2 instance's `Usdc` key names it (read-only probe, 2026-10-06).
TESTNET_NATIVE_SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"


@pytest.mark.parametrize(
    ("amount", "stroops"),
    [
        (0.012, 120_000),
        (0.054, 540_000),
        (0.18, 1_800_000),
        (0.0125, 125_000),
        (0.1234567, 1_234_567),
        (0, 0),
        (1, 10_000_000),
        ("0.0000001", 1),
        (Decimal("2.5"), 25_000_000),
    ],
)
def test_amounts_become_exact_stroops(amount: float | int | str | Decimal, stroops: int) -> None:
    assert money.to_stroops(amount) == stroops


def test_a_sum_of_float_prices_is_not_what_the_stroops_sum_to() -> None:
    """Why totals are summed in stroops: 0.1 + 0.2 is 0.30000000000000004 in floats."""
    prices = [0.1, 0.2]
    assert sum(prices) != 0.3
    assert sum(money.to_stroops(p) for p in prices) == money.to_stroops(0.3)


def test_more_than_seven_decimals_round_half_to_even() -> None:
    assert money.to_stroops("0.00000005") == 0
    assert money.to_stroops("0.00000015") == 2
    assert money.to_stroops("0.00000025") == 2
    assert money.to_stroops(0.00000015) == 2  # the written decimal, not the double's expansion


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), -0.01, -1, True, "abc", None, 2**127])
def test_what_the_ledger_cannot_hold_is_refused(bad: object) -> None:
    with pytest.raises(money.MoneyError):
        money.to_stroops(bad)  # type: ignore[arg-type]


def test_money_errors_are_value_errors() -> None:
    """Every caller that refused a bad amount with `except ValueError` still does."""
    assert issubclass(money.MoneyError, ValueError)


def test_the_legacy_float_round_trips_to_the_same_stroops() -> None:
    rng = random.Random(15)
    samples = [0, 1, 7, 120_000, 1_234_567, 10_000_000 * 10_000] + [rng.randrange(0, 10**12) for _ in range(20_000)]
    for n in samples:
        assert money.to_stroops(money.stroops_to_float(n)) == n


def test_from_stroops_is_exact() -> None:
    assert money.from_stroops(540_000) == Decimal("0.054")
    assert format(money.from_stroops(1), "f") == "0.0000001"


@pytest.mark.parametrize(
    ("stroops", "shown"),
    [
        (540_000, "0.054"),
        (125_000, "0.0125"),
        (1_234_567, "0.1234567"),
        (0, "0.000"),
        (10_000_000, "1.000"),
        (1, "0.0000001"),
    ],
)
def test_display_keeps_every_decimal_that_carries_something(stroops: int, shown: str) -> None:
    assert money.format_amount(stroops) == shown


@pytest.mark.parametrize("bad", [-1, 1.5, True, 2**127])
def test_display_refuses_what_is_not_stroops(bad: object) -> None:
    with pytest.raises(money.MoneyError):
        money.format_amount(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stroops", "fraction", "share"),
    [(540_000, 1.0, 540_000), (540_000, 0.5, 270_000), (3, 0.5, 1), (1, 0.9, 0), (10, 2.0, 10), (10, -1.0, 0)],
)
def test_a_fraction_rounds_down_and_is_clamped(stroops: int, fraction: float, share: int) -> None:
    assert money.fraction_of(stroops, fraction) == share


def test_a_non_finite_fraction_is_refused() -> None:
    with pytest.raises(money.MoneyError):
        money.fraction_of(10, float("nan"))


def test_the_testnet_native_sac_is_derived_not_assumed() -> None:
    assert money.native_sac_id(TESTNET) == TESTNET_NATIVE_SAC


def test_testnet_settles_in_native_xlm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_network_passphrase", TESTNET)
    monkeypatch.setattr(settings, "stellar_asset_sac", TESTNET_NATIVE_SAC)
    assert money.configured_asset() == money.AssetInfo(code="XLM", issuer=None, decimals=7)


def test_no_sac_configured_is_the_native_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_asset_sac", "")
    assert money.configured_asset() == money.NATIVE


def test_another_sac_is_read_once_and_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.stellar import client as sc

    sac = "CUSDCSACUSDCSACUSDCSACUSDCSACUSDCSACUSDCSACUSDCSACUSDCSA"
    issuer = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
    calls: list[tuple[str, str]] = []

    def fake_read(contract_id: str, fn: str, args: list[object]) -> str:
        calls.append((contract_id, fn))
        return f"USDC:{issuer}"

    monkeypatch.setattr(settings, "stellar_asset_sac", sac)
    monkeypatch.setattr(sc, "simulate_read", fake_read)
    monkeypatch.setattr(money, "_sac_assets", {})

    assert asyncio.run(money.current_asset()) == money.AssetInfo(code="USDC", issuer=issuer)
    assert asyncio.run(money.current_asset()) == money.AssetInfo(code="USDC", issuer=issuer)
    assert calls == [(sac, "name")]


def test_an_unreadable_sac_is_unknown_never_usdc(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.stellar import client as sc

    def broken(*_: object) -> str:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(settings, "stellar_asset_sac", "CSOMEOTHERSACSOMEOTHERSACSOMEOTHERSACSOMEOTHERSACSOMEOTH")
    monkeypatch.setattr(sc, "simulate_read", broken)
    monkeypatch.setattr(money, "_sac_assets", {})

    assert asyncio.run(money.current_asset()) == money.UNKNOWN
    assert money._sac_assets == {}  # not remembered: the next call asks again


@pytest.mark.parametrize(
    ("name", "asset"),
    [
        ("native", money.NATIVE),
        ("USDC:GISSUER", money.AssetInfo(code="USDC", issuer="GISSUER")),
        ("garbage", money.UNKNOWN),
        (":G", money.UNKNOWN),
    ],
)
def test_sac_names_parse(name: str, asset: money.AssetInfo) -> None:
    assert money.asset_from_sac_name(name) == asset
