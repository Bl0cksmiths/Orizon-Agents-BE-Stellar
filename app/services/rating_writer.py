"""Can this deployment write ratings? The rating writer's verdict.

`ReputationLedger.submit` accepts a rating only when `caller == Scorer`, the
address the ledger keeps in its instance storage — set by its constructor and
changed only by the admin's `set_scorer`. The backend signs every rating with
STELLAR_SIGNING_KEY, and only on wallet-authorized runs: a simulated run never
rates, by design. So three things have to line up before one rating lands —
reputation on with a ledger configured, a signing key, and that key being the
ledger's Scorer. The first two are config. The third exists only on the chain.

Each used to fail in silence. `_submit_ratings` returned without a word when
the config was incomplete; a key that was not the Scorer reverted every submit
with `Unauthorized` while /readiness reported `signer: configured`; and a
failed submit's trace line carried no reason. The testnet ledger sat at zero
ratings and nobody could say which of the three it was without Render's logs,
which the free tier loses on every restart.

This module gives the question a closed set of answers:

  disabled     REPUTATION_ENABLED is off, or STELLAR_REPUTATION_LEDGER is unset
  no_signer    no usable STELLAR_SIGNING_KEY
  scorer       the signer IS the ledger's stored Scorer — ratings can land
  not_scorer   it is not, or the ledger stores none — every submit will revert
  unchecked    the stored Scorer could not be read — never guessed

and says it in three places: one line at boot, `ratings` on /readiness, and a
reason on every rating a paid run fails to write.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from ..config import settings
from ..stellar import client as sc

logger = logging.getLogger(__name__)

WriterStatus = Literal["disabled", "no_signer", "scorer", "not_scorer", "unchecked"]


@dataclass(frozen=True)
class ConfigGap:
    """A setting whose absence stops this deployment writing any rating."""

    status: WriterStatus  # "disabled" or "no_signer"
    # For operators: names the setting and what is wrong with it.
    problem: str
    # For the task trace, which the buyer reads: names no setting or value.
    reason: str


_REPUTATION_OFF = ConfigGap("disabled", "REPUTATION_ENABLED is false", "reputation is disabled")
_NO_LEDGER = ConfigGap("disabled", "STELLAR_REPUTATION_LEDGER is unset", "no reputation ledger is configured")
_NO_KEY = ConfigGap("no_signer", "STELLAR_SIGNING_KEY is unset", "no signing key is configured")


def config_gap() -> ConfigGap | None:
    """The first missing setting that stops ratings, or None when all are set.

    Presence only, in the order an operator would fix them — and exactly the
    gate `_submit_ratings` applies, so the startup line, /readiness and a
    paid run's trace name the same setting for the same deployment.
    """
    if not settings.reputation_enabled:
        return _REPUTATION_OFF
    if not settings.stellar_reputation_ledger:
        return _NO_LEDGER
    if not settings.stellar_signing_key:
        return _NO_KEY
    return None


# ── the chain read ──────────────────────────────────────────────
# The stored Scorer changes only when the ledger admin calls `set_scorer`, or
# when the ledger is redeployed — and a redeploy is a new contract id, which
# the cache below does not match, so it is read at once. The TTL therefore
# only bounds how long a verdict can lag a `set_scorer`: five minutes is short
# enough that an operator fixing a mismatch sees /readiness agree before
# leaving the console, and one getLedgerEntries per five minutes — made only
# when someone asks — is nothing to the rate-limited SDF RPC.
SCORER_TTL_SECONDS = 300.0
# A failed read is retried sooner: an RPC blip at boot must not pin
# `unchecked` for five minutes, and a probe polled through an outage still
# costs at most one read per 30 s.
UNCHECKED_RETRY_SECONDS = 30.0
# Wall-clock bound on one read. The client's read profile caps each HTTP
# phase at 5 s, but per phase — connect and read separately — and not DNS;
# this is the promise that a verdict resolves, to `unchecked` if it must,
# instead of hanging. Above 5 s so a slow but live RPC still gets to answer.
SCORER_READ_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class _ScorerRead:
    """One attempt to read the ledger's stored Scorer, and how it ended."""

    ledger: str  # the contract id read — a changed id makes this read moot
    at: float  # time.monotonic() when the attempt resolved
    scorer: str | None = None  # on success: the Scorer, None if none is stored
    # On failure: "timed out after 8s" or an exception TYPE name — never the
    # exception's text, so the cause is safe to repeat anywhere.
    error: str | None = None


# The last attempt, whatever its outcome. None until the first one resolves.
_last_read: _ScorerRead | None = None


async def _read_scorer(ledger: str) -> None:
    """Read `ledger`'s stored Scorer once and remember the outcome. Never raises.

    `sc.ledger_scorer` is blocking (stellar-sdk is synchronous), so it runs on
    the bounded executor. `wait_for` stops waiting at the bound; the worker
    thread it leaves behind runs on until the client's own HTTP timeouts end
    it, which is why this bound sits above theirs rather than replacing them.
    """
    global _last_read
    try:
        scorer = await asyncio.wait_for(asyncio.to_thread(sc.ledger_scorer, ledger), SCORER_READ_TIMEOUT_SECONDS)
    except TimeoutError:
        _last_read = _ScorerRead(ledger, time.monotonic(), error=f"timed out after {SCORER_READ_TIMEOUT_SECONDS:g}s")
    except Exception as e:
        # The client's RPC span has already logged this at ERROR, text and
        # all; the verdict keeps the type alone.
        _last_read = _ScorerRead(ledger, time.monotonic(), error=type(e).__name__)
    else:
        _last_read = _ScorerRead(ledger, time.monotonic(), scorer=scorer)


# ── the verdict ─────────────────────────────────────────────────

# A key that is set but does not parse. Deliberately not one of config_gap()'s
# answers: that gate is presence-only, like `_submit_ratings` has always been,
# so a malformed key still attempts each submit and fails it one by one — the
# verdict is where it is named up front.
_BAD_KEY = ConfigGap(
    "no_signer",
    "STELLAR_SIGNING_KEY is set but is neither an S… secret nor a 12/24-word mnemonic",
    "the signing key is unusable",
)


@dataclass(frozen=True)
class WriterVerdict:
    """What this process knows about its ability to write ratings."""

    status: WriterStatus
    # The G… address ratings are signed with — set exactly when the chain
    # decides (scorer / not_scorer / unchecked). A public key, never the secret.
    signer: str | None = None
    # The ledger's stored Scorer as last read — set only when a read found one.
    scorer: str | None = None
    # What stopped it, for disabled / no_signer.
    gap: ConfigGap | None = None
    # Why the Scorer is unknown, for unchecked. For the log line only.
    read_error: str | None = None


def _signer() -> str | None:
    """The public key ratings are signed with, or None when the key does not parse.

    The same cached keypair `submit_rating` signs with, so the verdict judges
    the key actually in use. The exception is swallowed unread:
    `_signer_keypair` interpolates stellar_sdk's message, which quotes the
    rejected seed back.
    """
    try:
        return sc.signer_public_key()
    except Exception:
        return None


def verdict() -> WriterVerdict:
    """The verdict from config and the last chain read. Never does I/O.

    A read older than its TTL is still reported — it is the best this process
    knows — until `check()` or `refresh_if_stale()` replaces it.
    """
    gap = config_gap()
    if gap is not None:
        return WriterVerdict(gap.status, gap=gap)
    signer = _signer()
    if signer is None:
        return WriterVerdict("no_signer", gap=_BAD_KEY)
    read = _last_read
    if read is None or read.ledger != settings.stellar_reputation_ledger:
        return WriterVerdict("unchecked", signer=signer, read_error="not read yet")
    if read.error is not None:
        return WriterVerdict("unchecked", signer=signer, read_error=read.error)
    return WriterVerdict(
        "scorer" if read.scorer == signer else "not_scorer",
        signer=signer,
        scorer=read.scorer,
    )


# ── keeping it current ──────────────────────────────────────────

# The read in flight, shared by everyone who wants one — the startup check and
# any number of probes — so concurrent askers cost a single RPC call.
_read_task: asyncio.Task[None] | None = None


def _needs_read() -> bool:
    """Whether the chain decides the verdict and the cached read cannot answer."""
    if config_gap() is not None or _signer() is None:
        return False
    read = _last_read
    if read is None or read.ledger != settings.stellar_reputation_ledger:
        return True
    ttl = SCORER_TTL_SECONDS if read.error is None else UNCHECKED_RETRY_SECONDS
    return time.monotonic() - read.at >= ttl


def _start_read() -> asyncio.Task[None]:
    """The read in flight on this loop, starting one if there is none."""
    global _read_task
    loop = asyncio.get_running_loop()
    task = _read_task
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_read_scorer(settings.stellar_reputation_ledger))
        _read_task = task
    return task


async def check() -> WriterVerdict:
    """The verdict, reading the chain first when the cached read cannot answer.

    Bounded by SCORER_READ_TIMEOUT_SECONDS and never raises: a read that fails
    or times out is the `unchecked` verdict, not an exception.
    """
    if _needs_read():
        await _start_read()
    return verdict()


def refresh_if_stale() -> None:
    """Start a background read when the cached one cannot answer. Never waits.

    For /readiness, which stays free of live network calls: the probe reports
    what is cached, and this is what makes the next probe current.
    """
    if _needs_read():
        _start_read()
