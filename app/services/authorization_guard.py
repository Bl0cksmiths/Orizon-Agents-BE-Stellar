"""
The execute authorization guard (story 5.01, seam audit S2, ADR 0011).

`/execute` used to take any `auth_id_hex` and `payer` on trust. Under
PaymentEscrow v2 both are public — they sit in every `authd` event — so anyone
could point THEIR plan at a victim's authorization and have the first `settle`
pay the agents they chose, up to the victim's `max_amount`. Even on v1, where
the charge cannot land, an execute with a made-up authorization still wrote
on-chain ratings.

v2 closes it by making the authorization's label (`agent_id` in `authorize`)
the PLAN ID the buyer is paying for. This module reads the authorization back
from the configured escrow — a read-only simulate, nothing signed — and refuses
to run a plan against one that is not this payer's, not for this plan, already
spent, too small, or about to expire. Every refusal is a coded 4xx in the app's
error envelope; a chain that cannot be read is a 503, never a pass.

Against a v1 escrow nothing is refused: v1 holds no custody and its charge
cannot settle, so there is nothing to protect and the live demo keeps its
runs. That is logged on every paid execute, so it is never silent.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..schemas import StoredPlan
from ..security import CodedHTTPException
from ..state import state
from ..stellar import client as sc

logger = logging.getLogger(__name__)

STROOPS_PER_USDC = 10_000_000

# How long one read may take before the guard gives up and answers 503. The
# read runs on a worker thread that cannot be cancelled, so this bounds the
# REQUEST, not the thread; the RPC client's own timeout bounds the thread.
READ_TIMEOUT_SECONDS = 10.0

# The contract's `NotFound` (escrow-v2-interface.md, Errors).
_NOT_FOUND = 2

_CONTRACT_ERROR_HEAD = re.compile(r"\s*HostError: Error\(Contract, #(\d+)\)")
# What the host answers for a function the contract does not have. v1 never
# had `version()`, so this is the definite answer "1". Both parts are
# required: `MissingValue` alone is a broader host error.
_MISSING_FUNCTION_HEAD = re.compile(r"\s*HostError: Error\(WasmVm, MissingValue\)")
_MISSING_FUNCTION_CAUSE = "non-existent contract function"

# Contract id -> the version its `version()` answered. A contract id never
# changes version (a new escrow is a new id), so a DEFINITE answer is kept for
# the life of the process. An unreadable one is never kept.
_versions: dict[str, int] = {}

_REAUTHORIZE = "authorize this plan again and execute with the new authorization"


class AuthorizationRefused(CodedHTTPException):
    """`/execute` (or a reclaim build) refused an authorization. Coded, 4xx or 503.

    The message says what to do next and nothing more: every fact it rests on
    is already public in the escrow's `authd` event.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code, code, message)
        self.code = code


def _unverifiable(what: str) -> AuthorizationRefused:
    logger.warning("authorization unverifiable: %s", what)
    return AuthorizationRefused(
        503, "authorization_unverifiable", "the authorization could not be checked on-chain — try again shortly"
    )


@dataclass(frozen=True)
class OnChainAuthorization:
    """`authorization(auth_id)` as v2 returns it. Amounts in stroops, time in epoch seconds."""

    payer: str
    label: str  # the `agent_id` it was authorized under: the plan id on v2
    max_amount: int
    spent: int
    expires_at: int
    revoked: bool  # reclaimed by the payer
    settled: bool


@dataclass(frozen=True)
class Verified:
    """What `verify` established about an authorization.

    `authorization` is None exactly when the escrow is v1, where nothing is
    read and nothing enforced; `enforced` says which.
    """

    auth_id_hex: str
    escrow_version: int
    authorization: OnChainAuthorization | None

    @property
    def enforced(self) -> bool:
        return self.authorization is not None


def _wall_clock() -> float:
    """`time.time`, behind a seam the expiry tests can pin."""
    return time.time()


def _simulate_error(e: Exception) -> str:
    return str(e).removeprefix("simulate failed: ")


async def _simulate(contract_id: str, function_name: str, args: list[Any]) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(sc.simulate_read, contract_id, function_name, args, load_source=False),
        timeout=READ_TIMEOUT_SECONDS,
    )


def _escrow_id() -> str:
    escrow = sc.contract_ids().payment_escrow
    if not escrow:
        # A paid run with no escrow cannot settle, but it would still rate:
        # the exact farming this guard exists to stop. Not a pass.
        raise _unverifiable("no PaymentEscrow is configured")
    return escrow


async def escrow_version() -> int:
    """The configured escrow's `version()`: 1 when it has none. Raises 503 when unreadable."""
    escrow = _escrow_id()
    cached = _versions.get(escrow)
    if cached is not None:
        return cached
    try:
        value = await _simulate(escrow, "version", [])
    except RuntimeError as e:
        text = _simulate_error(e)
        if not (_MISSING_FUNCTION_HEAD.match(text) and _MISSING_FUNCTION_CAUSE in text):
            raise _unverifiable(f"PaymentEscrow {escrow} version() failed: {text[:200]}") from e
        version = 1
    except Exception as e:
        raise _unverifiable(f"PaymentEscrow {escrow} version() failed: {type(e).__name__}") from e
    else:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _unverifiable(f"PaymentEscrow {escrow} version() returned {value!r}")
        version = value
    _versions[escrow] = version
    return version


async def read_authorization(auth_id_hex: str) -> OnChainAuthorization | None:
    """Read one v2 authorization; None when the escrow answers `NotFound`. Raises 503 otherwise."""
    escrow = _escrow_id()
    try:
        record = await _simulate(escrow, "authorization", [sc.bytes16(bytes.fromhex(auth_id_hex))])
    except RuntimeError as e:
        match = _CONTRACT_ERROR_HEAD.match(_simulate_error(e))
        if match and int(match.group(1)) == _NOT_FOUND:
            return None
        raise _unverifiable(f"authorization {auth_id_hex} unreadable: {_simulate_error(e)[:200]}") from e
    except Exception as e:
        raise _unverifiable(f"authorization {auth_id_hex} unreadable: {type(e).__name__}") from e
    try:
        return _parse_authorization(record)
    except (KeyError, TypeError, ValueError) as e:
        raise _unverifiable(f"authorization {auth_id_hex} is not the v2 shape: {e!r}") from e


def _parse_authorization(record: Any) -> OnChainAuthorization:
    """Strict: a field of the wrong type is refused, never coerced into a pass."""
    if not isinstance(record, dict):
        raise TypeError(f"authorization() returned {type(record).__name__}")

    def integer(name: str) -> int:
        value = record[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} is {type(value).__name__}")
        return value

    def flag(name: str) -> bool:
        value = record[name]
        if not isinstance(value, bool):
            raise TypeError(f"{name} is {type(value).__name__}")
        return value

    def text(name: str) -> str:
        value = record[name]
        if not isinstance(value, str):
            raise TypeError(f"{name} is {type(value).__name__}")
        return value

    return OnChainAuthorization(
        payer=text("payer"),
        label=text("agent_id"),
        max_amount=integer("max_amount"),
        spent=integer("spent"),
        expires_at=integer("expires_at"),
        revoked=flag("revoked"),
        settled=flag("settled"),
    )


def required_stroops(plan: StoredPlan) -> int | None:
    """The least `max_amount` that can pay for `plan`, in stroops; None if nothing can.

    Two readings, and the larger wins. The per-step sum is what `settle` pays
    (each step's price in stroops). The plan total is what the buyer was
    quoted, taken to the next whole stroop — rounded UP, because a total
    rounded down to a stroop would let an authorization one stroop short
    through. The `round(…, 6)` first strips float noise (0.012 * 1e7 is
    120000.00000000001) so an exact total is not pushed up a stroop by it.
    """
    per_step = 0
    for step in plan.plan.steps:
        price = step.est_price_usdc
        if not math.isfinite(price) or price < 0:
            return None
        per_step += sc.usdc_to_i128(price)
    total = plan.total_usdc
    if not math.isfinite(total) or total < 0:
        return None
    return max(per_step, math.ceil(round(total * STROOPS_PER_USDC, 6)))


def run_margin_seconds(step_count: int) -> float:
    """How long an authorization must still live for a run of `step_count` steps to settle.

    `settle` is allowed after `expires_at`, but so is the payer's `reclaim`,
    and whichever lands first wins. A run that outlives its authorization is
    therefore a race its operators can lose — or a payer can win on purpose, by
    reclaiming the moment the clock passes. So the whole worst-case run has to
    fit before expiry. The settle lane's own figure is used when it exists, so
    the two checks can never disagree; the fallback is the same sum.
    """
    from . import execution_svc

    worst_case = getattr(execution_svc, "worst_case_run_seconds", None)
    if callable(worst_case):
        return float(worst_case(step_count))
    return (
        settings.reputation_batch_timeout_seconds
        + step_count * (execution_svc.STEP_TIMEOUT_SECONDS + 5.0)  # the step deadline plus its dispatch overhead
        + 150.0  # the settle's submit and confirmation
    )


def check_ownership(auth: OnChainAuthorization, *, auth_id_hex: str, payer: str, plan_id: str) -> None:
    """Refuse an authorization that is not this payer's, not for this plan, or already spent."""
    if auth.payer != payer:
        logger.warning("authorization %s refused: made by %s, presented for %s", auth_id_hex, auth.payer, payer)
        raise AuthorizationRefused(
            403, "authorization_payer_mismatch", "this authorization was made by a different wallet"
        )
    if auth.label != plan_id:
        # The S2 attack itself: someone else's (public) authorization, pointed
        # at a plan of the attacker's choosing.
        logger.warning("authorization %s refused: made for %s, presented for %s", auth_id_hex, auth.label, plan_id)
        raise AuthorizationRefused(
            403, "authorization_plan_mismatch", f"this authorization was made for a different plan — {_REAUTHORIZE}"
        )
    if auth.settled:
        raise AuthorizationRefused(
            409, "authorization_settled", f"this authorization has already been settled — {_REAUTHORIZE}"
        )
    if auth.revoked:
        raise AuthorizationRefused(
            409, "authorization_revoked", f"this authorization was reclaimed by its payer — {_REAUTHORIZE}"
        )


def check_fit(auth: OnChainAuthorization, *, auth_id_hex: str, plan: StoredPlan) -> None:
    """Refuse an authorization too small for `plan`, or too close to expiry to outlive a run of it."""
    required = required_stroops(plan)
    if required is None or auth.max_amount < required:
        logger.warning(
            "authorization %s refused: max %d stroops, plan %s needs %s",
            auth_id_hex,
            auth.max_amount,
            plan.id,
            required,
        )
        raise AuthorizationRefused(
            409, "authorization_insufficient", f"this authorization does not cover the plan's total — {_REAUTHORIZE}"
        )
    remaining = auth.expires_at - _wall_clock()
    needed = run_margin_seconds(len(plan.plan.steps))
    if remaining < needed:
        logger.warning(
            "authorization %s refused: %.0fs left, a %d-step run can need %.0fs",
            auth_id_hex,
            remaining,
            len(plan.plan.steps),
            needed,
        )
        raise AuthorizationRefused(
            409,
            "authorization_expired",
            f"this authorization expires before a run of this plan could be paid for — {_REAUTHORIZE}",
        )


async def _read_enforced(auth_id_hex: str) -> tuple[int, OnChainAuthorization | None]:
    """(version, authorization). The authorization is None on v1; a missing one is a 404."""
    version = await escrow_version()
    if version == 1:
        logger.warning(
            "paid execute against a v1 PaymentEscrow: authorization %s is NOT verified (v1 holds no custody)",
            auth_id_hex,
        )
        return 1, None
    if version != 2:
        raise _unverifiable(f"PaymentEscrow version {version} is not one this guard knows")
    auth = await read_authorization(auth_id_hex)
    if auth is None:
        raise AuthorizationRefused(404, "authorization_not_found", f"no such authorization — {_REAUTHORIZE}")
    return 2, auth


async def verify_ownership(auth_id_hex: str, payer: str, plan_id: str) -> Verified:
    """`verify` without the plan-fit checks, for a plan that no longer exists.

    Proves only that the authorization is live and was made by `payer` for
    `plan_id` — which is all a release needs, since a release returns the
    custody to that same payer and nobody else.
    """
    key = auth_id_hex.lower()
    version, auth = await _read_enforced(key)
    if auth is not None:
        check_ownership(auth, auth_id_hex=key, payer=payer, plan_id=plan_id)
    return Verified(auth_id_hex=key, escrow_version=version, authorization=auth)


async def verify(auth_id_hex: str, payer: str, plan: StoredPlan) -> Verified:
    """Check that the authorization can pay for THIS payer's run of THIS plan.

    Raises `AuthorizationRefused` for every failure; returns only when the run
    may start. On v1 it returns an unenforced result and refuses nothing.
    """
    key = auth_id_hex.lower()
    version, auth = await _read_enforced(key)
    if auth is not None:
        check_ownership(auth, auth_id_hex=key, payer=payer, plan_id=plan.id)
        check_fit(auth, auth_id_hex=key, plan=plan)
    return Verified(auth_id_hex=key, escrow_version=version, authorization=auth)


def forget_versions() -> None:
    """Drop every cached escrow version (tests; a process never needs it)."""
    _versions.clear()


# ── one authorization, one task ─────────────────────────────────────────
# A v2 authorization is custody for ONE run: the first `settle` spends it and
# every later one is a `Replay`. A second execute against it would run a whole
# plan — every step dispatched, every operator's work done — that nobody can
# pay for, and while the first run is still going it would race the first
# run's settle. So each authorization may start one task in this process.
#
# The durable half is the chain itself. A run that settled left the
# authorization `settled`, and one the payer reclaimed left it `revoked`, and
# `check_ownership` refuses both on every execute, after a restart included.
# What only this process knows is the window before that: a task that is
# still running, or one that finished without a settle landing. That is what
# the claims below hold. One worker (render.yaml `--workers 1`) makes this
# process the whole service; a second worker would have its own claims and
# could start a second run, so scaling out needs a shared claim (ADR 0011).

# The most claims kept. A claim is only ever made for an authorization the
# chain verified — real custody the payer locked — so the map is bounded by
# what callers have actually paid into the escrow; the cap is hygiene on top.
# A running task's claim is never the one evicted.
MAX_CLAIMS = 4096

# auth id (lowercase hex) -> the task it started, oldest first.
_claims: OrderedDict[str, str] = OrderedDict()
# One lock per authorization under check, and how many requests hold or await
# it, so the entry goes when the last of them leaves and the map stays bounded
# by the requests in flight.
_locks: dict[str, asyncio.Lock] = {}
_lock_users: dict[str, int] = {}


@asynccontextmanager
async def exclusive(auth_id_hex: str) -> AsyncIterator[None]:
    """Serialise every execute against one authorization, in this process.

    Held across the claim check, the on-chain read, the task mint and the
    claim, so two concurrent executes against one authorization cannot both
    pass the check before either records its claim — the read in between is
    an await, which is exactly where the event loop would interleave them.
    Executes against DIFFERENT authorizations never wait on each other.
    """
    key = auth_id_hex.lower()
    lock = _locks.setdefault(key, asyncio.Lock())
    _lock_users[key] = _lock_users.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _lock_users[key] -= 1
        if _lock_users[key] == 0:
            del _lock_users[key]
            _locks.pop(key, None)


def claimed_by(auth_id_hex: str) -> str | None:
    """The task this authorization already started in this process, if any."""
    return _claims.get(auth_id_hex.lower())


def refuse_if_claimed(auth_id_hex: str) -> None:
    """409 `authorization_used` when the authorization already started a task."""
    task_id = claimed_by(auth_id_hex)
    if task_id is None:
        return
    task = state.tasks.get(task_id)
    logger.warning(
        "execute refused: authorization %s already started task %s (%s)",
        auth_id_hex.lower(),
        task_id,
        task.status if task is not None else "evicted",
    )
    raise AuthorizationRefused(
        409, "authorization_used", f"this authorization has already paid for a run — {_REAUTHORIZE}"
    )


def _running(task_id: str) -> bool:
    task = state.tasks.get(task_id)
    return task is not None and task.status == "running"


def claim(auth_id_hex: str, task_id: str) -> None:
    """Record that `auth_id_hex` started `task_id`. Call with `exclusive` held."""
    _claims[auth_id_hex.lower()] = task_id
    while len(_claims) > MAX_CLAIMS:
        evict = next((a for a, t in _claims.items() if not _running(t)), next(iter(_claims)))
        del _claims[evict]


def forget_claims() -> None:
    """Drop every claim (tests; a process never needs it)."""
    _claims.clear()
