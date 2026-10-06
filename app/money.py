"""Money on this platform: integer stroops, one conversion rule, the real asset.

Every amount the escrow moves is an i128 in the token's smallest unit, and the
token is a Stellar asset with 7 decimals — on testnet the NATIVE asset (XLM),
wrapped by its Stellar Asset Contract. So the one exact representation of a
price is an integer count of stroops (10^-7 of a unit), and every other number
the API shows is derived from it (ADR 0015):

  * `to_stroops` is the ONE way a decimal amount becomes stroops. It goes
    through `Decimal` built from the float's shortest repr — the number the
    registry, the seed data or a request actually wrote — never through
    `amount * 10_000_000` on binary floats, and it refuses what the ledger
    cannot hold (non-finite, negative, past i128) instead of rounding it into
    something payable.
  * `stroops_to_float` is the ONE way stroops become a legacy float field
    (`est_price_usdc`, `total_usdc`): the double nearest the exact decimal, so
    `to_stroops(stroops_to_float(n)) == n` for every amount the platform can
    charge.
  * `format_amount` is the ONE display rule: all 7 decimals when they carry
    something, never fewer than 3, so a 0.0125 price is never shown as 0.013.
  * `current_asset` says what those stroops ARE, from the network config,
    so nothing labels native XLM as "USDC".

Rounding: a decimal amount with more than 7 places is rounded half-to-even to
the stroop. No price the platform holds has more than 7 (the registry stores
stroops, the seed data has 3), so the rule only decides a request's own input.
"""

from __future__ import annotations

import asyncio
import logging
import math
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from functools import lru_cache
from typing import Final

from pydantic import BaseModel, Field

from .config import settings

logger = logging.getLogger(__name__)

DECIMALS: Final = 7
STROOPS_PER_UNIT: Final = 10**DECIMALS
# Soroban's i128, the escrow's amount type. Nothing near it is a price, but the
# bound is the ledger's, so it is the one this module enforces.
I128_MAX: Final = 2**127 - 1

_QUANTUM: Final = Decimal(1).scaleb(-DECIMALS)  # Decimal("1E-7")
_MIN_DISPLAY_DECIMALS: Final = 3


class MoneyError(ValueError):
    """An amount the ledger cannot hold: not a number, negative, or too large.

    A `ValueError`, so every caller that already refused a bad amount with
    `except ValueError` keeps refusing it.
    """


def _decimal(amount: float | int | str | Decimal) -> Decimal:
    if isinstance(amount, bool):
        raise MoneyError(f"{amount!r} is not an amount")
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, float):
        if not math.isfinite(amount):
            raise MoneyError(f"{amount!r} is not a finite amount")
        # repr is the shortest string that round-trips: the decimal the
        # writer meant, not the binary double's expansion of it.
        value = Decimal(repr(amount))
    elif isinstance(amount, str):
        try:
            value = Decimal(amount.strip())
        except InvalidOperation as e:
            raise MoneyError(f"{amount!r} is not a decimal amount") from e
    else:
        raise MoneyError(f"{type(amount).__name__} is not an amount")
    if not value.is_finite():
        raise MoneyError(f"{amount!r} is not a finite amount")
    return value


def to_stroops(amount: float | int | str | Decimal) -> int:
    """`amount` (in whole units of the asset) as integer stroops, exactly.

    `0.054` → `540_000`. Rounded half-to-even to the stroop when it has more
    than 7 decimals. Raises `MoneyError` for anything non-finite, negative or
    beyond i128 — a price that cannot be paid is refused, never coerced.
    """
    value = _decimal(amount)
    if value < 0:
        raise MoneyError(f"{amount!r} is negative")
    stroops = int((value * STROOPS_PER_UNIT).to_integral_value(rounding=ROUND_HALF_EVEN))
    if stroops > I128_MAX:
        raise MoneyError(f"{amount!r} is beyond what the ledger can hold")
    return stroops


def from_stroops(stroops: int) -> Decimal:
    """`stroops` as an exact `Decimal` amount of the asset: `540_000` → `Decimal("0.0540000")`."""
    return _checked(stroops).scaleb(-DECIMALS).quantize(_QUANTUM)


def stroops_to_float(stroops: int) -> float:
    """`stroops` as the legacy float field: the double nearest the exact amount.

    Deprecated as a source of truth — kept for clients that read
    `est_price_usdc`/`total_usdc` — and derived from the integer, so it can
    never disagree with it: `to_stroops(stroops_to_float(n)) == n`.
    """
    return float(from_stroops(stroops))


def format_amount(stroops: int) -> str:
    """The display string for `stroops`: `540_000` → `"0.054"`, `125_000` → `"0.0125"`.

    Every non-zero decimal is kept (up to the asset's 7) and never fewer than
    3 are shown, so the string is exact and still reads like a price.
    """
    text = format(from_stroops(stroops), "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0").ljust(_MIN_DISPLAY_DECIMALS, "0")
    return f"{whole}.{frac}"


def fraction_of(stroops: int, fraction: float) -> int:
    """`fraction` of `stroops`, rounded DOWN to the stroop, `fraction` clamped to [0, 1].

    Down, because what this computes is a share somebody is promised (a
    dispute credit): rounding to nearest would hand out up to half a stroop
    more than the share. A non-finite fraction is refused.
    """
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction):
        raise MoneyError(f"{fraction!r} is not a finite fraction")
    share = min(max(Decimal(repr(float(fraction))), Decimal(0)), Decimal(1))
    return int((Decimal(_checked(stroops)) * share).to_integral_value(rounding=ROUND_DOWN))


def _checked(stroops: int) -> Decimal:
    if isinstance(stroops, bool) or not isinstance(stroops, int):
        raise MoneyError(f"{stroops!r} is not a whole number of stroops")
    if stroops < 0 or stroops > I128_MAX:
        raise MoneyError(f"{stroops} stroops is not an amount the ledger can hold")
    return Decimal(stroops)


class Amount(BaseModel):
    """One amount on the wire: exact `stroops`, and the `display` string a page shows.

    The display never rounds (see `format_amount`); the asset it is in is
    named once beside it by the enclosing payload, never assumed.
    """

    stroops: int = Field(ge=0)
    display: str

    @classmethod
    def of(cls, stroops: int) -> Amount:
        return cls(stroops=stroops, display=format_amount(stroops))


# ── the asset ──────────────────────────────────────────────────────────
class AssetInfo(BaseModel):
    """What a plan's stroops are stroops OF (`Plan.asset`).

    `code` is the asset code (`"XLM"` for the native asset), `issuer` its
    issuing account (null for native), `decimals` always 7 on Stellar. A
    client labels amounts with `code` and never assumes USDC.
    """

    code: str
    issuer: str | None = None
    decimals: int = Field(default=DECIMALS, ge=0)


NATIVE: Final = AssetInfo(code="XLM", issuer=None)
UNKNOWN: Final = AssetInfo(code="UNKNOWN", issuer=None)

# A configured non-native SAC, as its `name()` answered: fixed for the life of
# a contract id, so one successful read per id is the whole cost.
_sac_assets: dict[str, AssetInfo] = {}
_SAC_READ_TIMEOUT_SECONDS: Final = 3.0


@lru_cache(maxsize=8)
def native_sac_id(passphrase: str) -> str:
    """The contract id of the native asset's SAC on the network `passphrase` names. Pure."""
    from stellar_sdk import Asset

    return str(Asset.native().contract_id(passphrase))


def asset_from_sac_name(name: str) -> AssetInfo:
    """A SAC `name()` answer as an `AssetInfo`: `"native"` or `"CODE:ISSUER"`."""
    if name == "native":
        return NATIVE
    code, sep, issuer = name.partition(":")
    if not sep or not code or not issuer:
        return UNKNOWN
    return AssetInfo(code=code, issuer=issuer)


def configured_asset() -> AssetInfo | None:
    """The asset the configuration settles in, when it can be known WITHOUT a read.

    The native asset when `STELLAR_ASSET_SAC` is the network's native SAC —
    the testnet deployment — or when no SAC is configured at all (no paid run
    can happen then, and simulated runs are priced in the network's own
    asset). None when a different SAC is configured: only the chain knows
    what it wraps, and `current_asset` asks it.
    """
    sac = settings.stellar_asset_sac.strip()
    if not sac:
        return NATIVE
    if sac == native_sac_id(settings.stellar_network_passphrase):
        return NATIVE
    return _sac_assets.get(sac)


async def current_asset() -> AssetInfo:
    """The asset this deployment's escrow moves, from the live network config.

    Native XLM on testnet, decided from the configuration alone. For another
    SAC, its `name()` is read once (bounded) and remembered; a read that fails
    answers `UNKNOWN` — never a guess such as "USDC" — and is retried next time.
    """
    known = configured_asset()
    if known is not None:
        return known
    sac = settings.stellar_asset_sac.strip()
    from .stellar import client as sc

    try:
        name = await asyncio.wait_for(asyncio.to_thread(sc.simulate_read, sac, "name", []), _SAC_READ_TIMEOUT_SECONDS)
    except Exception as e:  # any failed read is the same "not known"
        logger.warning("asset of SAC %s unreadable: %s", sac, e)
        return UNKNOWN
    asset = asset_from_sac_name(name) if isinstance(name, str) else UNKNOWN
    if asset is not UNKNOWN:
        _sac_assets[sac] = asset
    return asset


def asset_code() -> str:
    """The code amounts are labelled with in text — `"XLM"` on testnet, never an assumed `"USDC"`."""
    return (configured_asset() or UNKNOWN).code
