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
import contextlib
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


# ── saying it at boot ───────────────────────────────────────────


def report(v: WriterVerdict) -> None:
    """Log the verdict as one line: INFO when ratings can land, WARNING otherwise.

    The healthy case speaks too, for the reason main.py's cold-start line does:
    a check that is heard only when it fails cannot be told apart from one
    that never ran. Every line names what an operator needs to act without
    opening source — both addresses when they disagree — and none can carry a
    secret: the signer is a public key, and read errors are type names.
    """
    ledger = settings.stellar_reputation_ledger
    if v.status == "scorer":
        logger.info(
            "ratings writer ok: signer %s is the Scorer of ReputationLedger %s, so wallet-authorized "
            "runs will write their ratings on-chain (simulated runs never rate, by design).",
            v.signer,
            ledger,
        )
    elif v.status == "not_scorer" and v.scorer is not None:
        logger.warning(
            "ratings writer BROKEN: STELLAR_SIGNING_KEY signs as %s, but ReputationLedger %s accepts "
            "ratings only from its Scorer %s. Every rating submit will revert with Unauthorized, so no "
            "paid run adds on-chain evidence and every agent stays on the prior — while /readiness "
            "still reports the signer configured. Fix it on-chain: the ledger admin calls "
            "set_scorer(%s). Swapping STELLAR_SIGNING_KEY instead would also change the settler and "
            "sealer.",
            v.signer,
            ledger,
            v.scorer,
            v.signer,
        )
    elif v.status == "not_scorer":
        logger.warning(
            "ratings writer BROKEN: ReputationLedger %s stores no Scorer on %s — no contract instance "
            "lives at that id on this network, or it is not a ReputationLedger. Every rating submit "
            "will fail, so paid runs will not be rated. Check STELLAR_REPUTATION_LEDGER against the "
            "ledger deployed for STELLAR_NETWORK=%s.",
            ledger,
            settings.stellar_network,
            settings.stellar_network,
        )
    elif v.status == "unchecked":
        logger.warning(
            "ratings writer unchecked: could not read the Scorer of ReputationLedger %s (%s), so "
            "whether signer %s may rate is unknown. Paid runs still submit their ratings, and if the "
            "signer is not the Scorer each one reverts with Unauthorized. /readiness retries the read "
            "and reports ratings.writer once it lands.",
            ledger,
            v.read_error,
            v.signer,
        )
    else:
        logger.warning(
            "ratings writer off (%s): %s, so wallet-authorized runs will not be rated and no agent "
            "earns on-chain evidence beyond the prior. Simulated runs never rate either, by design.",
            v.status,
            v.gap.problem if v.gap is not None else "configuration incomplete",
        )


# The boot-time check while it runs: held so it is not garbage collected
# mid-read, and so shutdown can cancel it.
_report_task: asyncio.Task[None] | None = None


async def _check_and_report() -> None:
    report(await check())


def start() -> None:
    """Check the verdict and log it, in the background (lifespan startup).

    Deliberately not awaited. uvicorn accepts no connection until lifespan
    startup returns, and on the free tier every wake from idle is a boot with
    the request that woke it still waiting — so a read here, up to
    SCORER_READ_TIMEOUT_SECONDS against a rate-limited public RPC, would be
    paid for by a user, for a log line. Nothing needs the verdict first
    either: it gates nothing, and `_submit_ratings` never consults it.
    """
    global _report_task
    if _report_task is not None and not _report_task.done():
        return
    _report_task = asyncio.get_running_loop().create_task(_check_and_report())
    _report_task.add_done_callback(_on_report_done)


def _on_report_done(task: asyncio.Task[None]) -> None:
    # registry_sync._on_task_done's reason: nothing awaits this task, so an
    # exception inside it would otherwise vanish without a line anywhere.
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("ratings writer startup check died: %s", exc, exc_info=exc)


async def stop() -> None:
    """Cancel the startup check and any read in flight (lifespan shutdown).

    Without this a shutdown inside a slow read leaves a pending task for the
    loop to destroy — the noise lifespan already goes out of its way to avoid.
    """
    global _report_task, _read_task
    loop = asyncio.get_running_loop()
    tasks = [t for t in (_report_task, _read_task) if t is not None and not t.done() and t.get_loop() is loop]
    _report_task = _read_task = None
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
