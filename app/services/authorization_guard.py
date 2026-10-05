"""
The execute authorization guard (story 5.01, seam audit S2, ADR 0011).

`/execute` used to take any `auth_id_hex` and `payer` on trust. Under
PaymentEscrow v2 both are public — they sit in every `authd` event — so anyone
could point THEIR plan at a victim's authorization and have the first `settle`
pay the agents they chose, up to the victim's `max_amount`. v2 closes it by
making the authorization's label (`agent_id` in `authorize`) the PLAN ID the
buyer is paying for.

The check that a run may START is `execution_svc._authorize_for_execute`,
inside `execute_plan`: one verifier, one read, one set of codes. What this
module adds around it, in the `/execute` route (ADR 0011):

  - the escrow VERSION, read before a paid run and never guessed: an
    unreadable one is a 503, not "v1";
  - one authorization, one task — a claim held under a per-authorization lock;
  - the custody release when the route refuses a run that can never happen
    (plan gone, plan expired, no capacity), which reads the authorization only
    to prove it is this payer's and labelled for this plan before handing it
    back;
  - the pre-check behind `POST /api/stellar/build/reclaim`.

Every refusal uses the settle lane's code for the same condition, so a client
sees one vocabulary. Every read is a read-only simulate; nothing is signed.
Against a v1 escrow nothing is claimed, released or refused.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from ..security import CodedHTTPException
from ..state import state
from ..stellar import client as sc

logger = logging.getLogger(__name__)

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


def _unreadable(what: str) -> AuthorizationRefused:
    logger.warning("authorization unreadable: %s", what)
    return AuthorizationRefused(
        503, "authorization_unreadable", "the authorization could not be read on-chain — try again shortly"
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
    """What `verify_ownership` established about an authorization.

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
        raise _unreadable("no PaymentEscrow is configured")
    return escrow


async def escrow_version() -> int:
    """The configured escrow's `version()`: 1 when it has none. Raises 503 when unreadable.

    Through the client's own `escrow_version` when this build has it, so the
    answer lands in the SAME cache `execute_plan` reads. That matters: the
    settle lane reads an unreadable version as 1 and skips its authorization
    check, so a paid execute must never reach it before a definite answer is
    cached. The fallback below is the same read for a build without it.
    """
    escrow = _escrow_id()
    shared = getattr(sc, "escrow_version", None)
    if callable(shared):
        try:
            value = await asyncio.wait_for(asyncio.to_thread(shared, escrow), timeout=READ_TIMEOUT_SECONDS)
        except Exception as e:
            raise _unreadable(f"PaymentEscrow {escrow} version() failed: {type(e).__name__}: {str(e)[:200]}") from e
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _unreadable(f"PaymentEscrow {escrow} version() returned {value!r}")
        return value
    cached = _versions.get(escrow)
    if cached is not None:
        return cached
    try:
        value = await _simulate(escrow, "version", [])
    except RuntimeError as e:
        text = _simulate_error(e)
        if not (_MISSING_FUNCTION_HEAD.match(text) and _MISSING_FUNCTION_CAUSE in text):
            raise _unreadable(f"PaymentEscrow {escrow} version() failed: {text[:200]}") from e
        version = 1
    except Exception as e:
        raise _unreadable(f"PaymentEscrow {escrow} version() failed: {type(e).__name__}") from e
    else:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _unreadable(f"PaymentEscrow {escrow} version() returned {value!r}")
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
        raise _unreadable(f"authorization {auth_id_hex} unreadable: {_simulate_error(e)[:200]}") from e
    except Exception as e:
        raise _unreadable(f"authorization {auth_id_hex} unreadable: {type(e).__name__}") from e
    try:
        return _parse_authorization(record)
    except (KeyError, TypeError, ValueError) as e:
        raise _unreadable(f"authorization {auth_id_hex} is not the v2 shape: {e!r}") from e


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
            409, "authorization_plan_mismatch", f"this authorization was made for a different plan — {_REAUTHORIZE}"
        )
    if auth.settled or auth.revoked:
        raise AuthorizationRefused(409, "authorization_spent", f"this authorization is already spent — {_REAUTHORIZE}")


async def _read_enforced(auth_id_hex: str) -> tuple[int, OnChainAuthorization | None]:
    """(version, authorization). The authorization is None on v1; a missing one is a 404."""
    version = await escrow_version()
    if version == 1:
        return 1, None
    if version != 2:
        raise _unreadable(f"PaymentEscrow version {version} is not one this guard knows")
    auth = await read_authorization(auth_id_hex)
    if auth is None:
        raise AuthorizationRefused(404, "authorization_not_found", f"no such authorization — {_REAUTHORIZE}")
    return 2, auth


async def verify_ownership(auth_id_hex: str, payer: str, plan_id: str) -> Verified:
    """Prove the authorization is live and was made by `payer` for `plan_id`; raise otherwise.

    Deliberately NOT the run's verification, which is `execute_plan`'s: no
    cap and no expiry, because a release needs neither. It returns the custody
    to that same payer and nobody else, so all it must establish is that the
    custody is this payer's, for this plan, and not already spent.
    """
    key = auth_id_hex.lower()
    version, auth = await _read_enforced(key)
    if auth is not None:
        check_ownership(auth, auth_id_hex=key, payer=payer, plan_id=plan_id)
    return Verified(auth_id_hex=key, escrow_version=version, authorization=auth)


def forget_versions() -> None:
    """Drop every cached escrow version, the client's shared cache included (tests; a process never needs it)."""
    _versions.clear()
    forget_shared = getattr(sc, "forget_escrow_versions", None)
    if callable(forget_shared):
        forget_shared()


# ── reclaim pre-check ───────────────────────────────────────────────────

# `reclaim` compares against the LEDGER's clock, which runs up to a close
# behind the wall clock. A build in that gap would be prepared, signed, and
# then refused on-chain as `Locked`; waiting this long past `expires_at` first
# keeps the answer honest.
LEDGER_CLOCK_ALLOWANCE_SECONDS = 10.0


async def check_reclaimable(auth_id_hex: str, payer: str) -> OnChainAuthorization:
    """Refuse, with the reason, a reclaim the contract would refuse; else the authorization.

    Mirrors `reclaim`'s own checks in its order — the payer (`Unauthorized`),
    settled (`Replay`), reclaimed (`Revoked`), not yet expired (`Locked`) — so
    the buyer learns why BEFORE signing, not from a failed transaction after.
    """
    version = await escrow_version()
    if version == 1:
        raise AuthorizationRefused(
            409, "reclaim_unsupported", "this escrow holds no custody, so there is nothing to reclaim"
        )
    if version != 2:
        raise _unreadable(f"PaymentEscrow version {version} is not one this guard knows")
    key = auth_id_hex.lower()
    auth = await read_authorization(key)
    if auth is None:
        raise AuthorizationRefused(404, "authorization_not_found", "no such authorization")
    if auth.payer != payer:
        raise AuthorizationRefused(
            403, "authorization_payer_mismatch", "only the wallet that made this authorization can reclaim it"
        )
    # A code each, as the route documents and the console maps them ("already
    # settled" / "already reclaimed" — answers, not errors). Execute keeps the
    # settle lane's shared `authorization_spent`: there both mean "authorize
    # again", and its clients already speak that code.
    if auth.settled:
        raise AuthorizationRefused(
            409, "authorization_settled", "this authorization was already settled — anything unspent was returned"
        )
    if auth.revoked:
        raise AuthorizationRefused(409, "authorization_revoked", "this authorization was already reclaimed")
    if _wall_clock() <= auth.expires_at + LEDGER_CLOCK_ALLOWANCE_SECONDS:
        # The instant is public on-chain, so saying it discloses nothing.
        raise AuthorizationRefused(
            409,
            "authorization_locked",
            f"this authorization can be reclaimed once it expires at {auth.expires_at} (unix time)",
        )
    return auth


# ── one authorization, one task ─────────────────────────────────────────
# A v2 authorization is custody for ONE run: the first `settle` spends it and
# every later one is a `Replay`. A second execute against it would run a whole
# plan — every step dispatched, every operator's work done — that nobody can
# pay for, and while the first run is still going it would race the first
# run's settle. So each authorization may start one task in this process.
#
# The durable half is the chain itself. A run that settled left the
# authorization `settled`, and one the payer reclaimed left it `revoked`, and
# `execute_plan` refuses both on every execute (`authorization_spent`), after a
# restart included.
# What only this process knows is the window before that: a task that is
# still running, or one that finished without a settle landing. That is what
# the claims below hold. One worker (render.yaml `--workers 1`) makes this
# process the whole service; a second worker would have its own claims and
# could start a second run, so scaling out needs a shared claim (ADR 0011).

# The most claims kept. A claim outlives its request only when `execute_plan`
# minted a task, which on v2 it does only for an authorization it verified —
# real custody the payer locked — so the map is bounded by what callers have
# actually paid into the escrow; the cap is hygiene on top. A running task's
# claim, or one still pending, is never the one evicted.
MAX_CLAIMS = 4096

# The claim taken BEFORE `execute_plan` is called, while no task id exists yet.
PENDING = "pending"

# auth id (lowercase hex) -> the task it started (or PENDING), oldest first.
_claims: OrderedDict[str, str] = OrderedDict()
# One lock per authorization under check, and how many requests hold or await
# it, so the entry goes when the last of them leaves and the map stays bounded
# by the requests in flight.
_locks: dict[str, asyncio.Lock] = {}
_lock_users: dict[str, int] = {}


@asynccontextmanager
async def exclusive(auth_id_hex: str) -> AsyncIterator[None]:
    """Serialise every execute against one authorization, in this process.

    Held across the claim check, the reads, `execute_plan` and the claim, so
    two concurrent executes against one authorization cannot both pass the
    check before either records its claim — every read in between is an
    await, which is exactly where the event loop would interleave them.
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
    if task_id == PENDING:
        return True
    task = state.tasks.get(task_id)
    return task is not None and task.status == "running"


def claim(auth_id_hex: str, task_id: str = PENDING) -> None:
    """Record that `auth_id_hex` started `task_id` (or is about to). Call with `exclusive` held."""
    _claims[auth_id_hex.lower()] = task_id
    while len(_claims) > MAX_CLAIMS:
        evict = next((a for a, t in _claims.items() if not _running(t)), next(iter(_claims)))
        del _claims[evict]


def unclaim(auth_id_hex: str) -> None:
    """Drop a claim whose `execute_plan` raised before minting a task. Call with `exclusive` held."""
    _claims.pop(auth_id_hex.lower(), None)


def forget_claims() -> None:
    """Drop every claim (tests; a process never needs it)."""
    _claims.clear()


# ── custody release on a refusal ────────────────────────────────────────


@dataclass(frozen=True)
class Release:
    """A release was attempted. `tx_hash` is set only when it CONFIRMED."""

    tx_hash: str | None


async def release(auth_id_hex: str, *, reason: str) -> str | None:
    """Hand the authorization's whole custody back to its payer. Never raises.

    Delegates to `execution_svc.release_authorization` (the settle lane's
    full-release `settle` with no payouts; a no-op on v1), looked up at call
    time: it is that lane's to write, and a deployment without it releases
    nothing rather than failing the request. Returns the tx hash when the
    release confirmed, else None.
    """
    from . import execution_svc

    release_authorization = getattr(execution_svc, "release_authorization", None)
    if release_authorization is None:
        logger.warning(
            "authorization %s not released (%s): no release_authorization in this build", auth_id_hex, reason
        )
        return None
    try:
        result = release_authorization(auth_id_hex, reason=reason)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        logger.exception(
            "authorization %s release (%s) raised; custody stays reclaimable after expiry", auth_id_hex, reason
        )
        return None
    return result if isinstance(result, str) and result else None


async def release_if_owned(auth_id_hex: str, payer: str, plan_id: str, *, reason: str) -> Release | None:
    """Release the authorization only if it is live and is `payer`'s, for `plan_id`.

    For a refusal the route makes before any run: the plan expired or is gone
    (the label can never match another plan), or the service had no capacity.
    None when nothing was attempted — v1, an unreadable chain, or an
    authorization that failed ownership. Never release
    what failed ownership: that custody may be someone else's, and whoever
    sent its public id has no say over it. Call with `exclusive` held and after
    `refuse_if_claimed`, so a claimed authorization — one funding a run — is
    never released under it.
    """
    try:
        verified = await verify_ownership(auth_id_hex, payer, plan_id)
    except AuthorizationRefused as e:
        logger.info("authorization %s not released (%s): %s", auth_id_hex.lower(), reason, e.code)
        return None
    return await release_verified(verified, reason=reason)


async def release_verified(verified: Verified, *, reason: str) -> Release | None:
    """Release an authorization `verify_ownership` passed; None (nothing attempted) on v1."""
    if not verified.enforced:
        return None
    tx_hash = await release(verified.auth_id_hex, reason=reason)
    logger.warning(
        "authorization %s released after a refused execute (%s): %s",
        verified.auth_id_hex,
        reason,
        f"tx {tx_hash}" if tx_hash else "did not confirm; reclaimable after expiry",
    )
    return Release(tx_hash=tx_hash)
