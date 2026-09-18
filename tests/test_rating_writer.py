"""Can this deployment write ratings? — the verdict, and how it is kept.

The testnet ReputationLedger held zero ratings and nobody could say why: the
config gate in `_submit_ratings` returned in silence, and a signing key that
is not the ledger's Scorer reverts every rating while /readiness reported the
signer "configured". `rating_writer` answers the question with a closed set —
disabled, no_signer, scorer, not_scorer, unchecked — and these pin each
answer, the cache that keeps the one chain read cheap, and the bound that
keeps it from hanging. The chain is always a stub: nothing here reaches the
network, and an unstubbed read fails the test.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from stellar_sdk import Keypair, StrKey

from app.config import settings
from app.services import rating_writer as rw
from app.stellar import client as sc

SIGNER = Keypair.from_raw_ed25519_seed(b"\x0a" * 32).public_key
OTHER = Keypair.from_raw_ed25519_seed(b"\x0b" * 32).public_key
LEDGER = StrKey.encode_contract(b"\x0c" * 32)


class Chain:
    """Stands in for `sc.ledger_scorer`: answers as told and counts reads."""

    def __init__(self, scorer: str | None = None, error: Exception | None = None) -> None:
        self.scorer = scorer
        self.error = error
        self.reads: list[str] = []

    def __call__(self, ledger: str) -> str | None:
        self.reads.append(ledger)
        if self.error is not None:
            raise self.error
        return self.scorer


@pytest.fixture(autouse=True)
def fresh_writer(monkeypatch):
    """No cached read, no task, no skip history — and no way to the network."""
    monkeypatch.setattr(rw, "_last_read", None)
    monkeypatch.setattr(rw, "_read_task", None)
    monkeypatch.setattr(rw, "_report_task", None)
    monkeypatch.setattr(rw, "_skip_warned_at", {})
    monkeypatch.setattr(rw, "_skips_unreported", {})

    def _unstubbed(ledger: str) -> str | None:
        raise AssertionError("a test reached the chain without stubbing it")

    monkeypatch.setattr(sc, "ledger_scorer", _unstubbed)


def configure(monkeypatch, *, enabled: bool = True, ledger: str = LEDGER, key: bool = True) -> None:
    """A deployment whose key signs as SIGNER, unless told otherwise."""
    monkeypatch.setattr(settings, "reputation_enabled", enabled)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", ledger)
    # Presence is all the config gate reads; the public key comes from the
    # stubbed keypair below, so no real secret is ever parsed here.
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present" if key else "")
    monkeypatch.setattr(sc, "signer_public_key", lambda: SIGNER)


def on_chain(monkeypatch, chain: Chain) -> Chain:
    monkeypatch.setattr(sc, "ledger_scorer", chain)
    return chain


# ── config decides: no chain read at all ────────────────────────


def test_reputation_switched_off_is_disabled(monkeypatch):
    configure(monkeypatch, enabled=False)
    v = asyncio.run(rw.check())
    assert v.status == "disabled"
    assert v.gap is not None and "REPUTATION_ENABLED" in v.gap.problem
    assert (v.signer, v.scorer) == (None, None)


def test_no_ledger_is_disabled(monkeypatch):
    configure(monkeypatch, ledger="")
    v = asyncio.run(rw.check())
    assert v.status == "disabled"
    assert v.gap is not None and "STELLAR_REPUTATION_LEDGER" in v.gap.problem


def test_no_signing_key_is_no_signer(monkeypatch):
    configure(monkeypatch, key=False)
    v = asyncio.run(rw.check())
    assert v.status == "no_signer"
    assert v.gap is not None and v.gap.problem == "STELLAR_SIGNING_KEY is unset"
    assert (v.signer, v.scorer) == (None, None)


def test_a_key_that_does_not_parse_is_no_signer_and_never_quoted(monkeypatch):
    """`_signer_keypair`'s message quotes stellar_sdk's, which quotes the
    seed. The verdict must carry none of it — not in the gap, not anywhere."""
    configure(monkeypatch)
    secret_ish = "SBADSEEDTHATMUSTNEVERAPPEAR"

    def _bad_key() -> str:
        raise RuntimeError(f"STELLAR_SIGNING_KEY must be an S… secret ({secret_ish})")

    monkeypatch.setattr(sc, "signer_public_key", _bad_key)
    v = asyncio.run(rw.check())
    assert v.status == "no_signer"
    assert v.gap is not None and "STELLAR_SIGNING_KEY is set but" in v.gap.problem
    assert secret_ish not in repr(v)


def test_the_config_gate_names_the_first_gap_an_operator_would_fix(monkeypatch):
    """The order `_submit_ratings` gates in, so every surface names the same
    setting for the same deployment."""
    configure(monkeypatch, enabled=False, ledger="", key=False)
    assert rw.config_gap() is rw._REPUTATION_OFF
    monkeypatch.setattr(settings, "reputation_enabled", True)
    assert rw.config_gap() is rw._NO_LEDGER
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    assert rw.config_gap() is rw._NO_KEY
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present")
    assert rw.config_gap() is None


def test_a_config_verdict_never_reads_the_chain(monkeypatch):
    """disabled and no_signer are known from config alone. The fixture's
    unstubbed read would fail this test if either went to the chain."""
    for kwargs in ({"enabled": False}, {"ledger": ""}, {"key": False}):
        configure(monkeypatch, **kwargs)
        assert asyncio.run(rw.check()).status in ("disabled", "no_signer")


# ── the chain decides ───────────────────────────────────────────


def test_a_signer_that_is_the_stored_scorer_is_scorer(monkeypatch):
    configure(monkeypatch)
    chain = on_chain(monkeypatch, Chain(scorer=SIGNER))
    v = asyncio.run(rw.check())
    assert (v.status, v.signer, v.scorer) == ("scorer", SIGNER, SIGNER)
    assert chain.reads == [LEDGER]


def test_a_signer_that_is_not_the_stored_scorer_is_not_scorer(monkeypatch):
    """The live failure: every submit would revert with Unauthorized. Both
    addresses are kept, because the fix is set_scorer(<signer>)."""
    configure(monkeypatch)
    on_chain(monkeypatch, Chain(scorer=OTHER))
    v = asyncio.run(rw.check())
    assert (v.status, v.signer, v.scorer) == ("not_scorer", SIGNER, OTHER)


def test_a_ledger_that_stores_no_scorer_is_not_scorer(monkeypatch):
    """No instance at that id on this network, or not a ReputationLedger:
    the chain answered, and no signer can rate against it."""
    configure(monkeypatch)
    on_chain(monkeypatch, Chain(scorer=None))
    v = asyncio.run(rw.check())
    assert (v.status, v.signer, v.scorer) == ("not_scorer", SIGNER, None)


def test_a_read_that_fails_is_unchecked_never_a_guess(monkeypatch):
    """Could-not-read is not evidence either way. Neither scorer nor
    not_scorer may come out of a failed read, and the cause kept for the log
    is the exception's type — its text stays in the client's own ERROR line."""
    configure(monkeypatch)
    on_chain(monkeypatch, Chain(error=ConnectionError("rpc down at https://rpc.example/?key=abc")))
    v = asyncio.run(rw.check())
    assert (v.status, v.signer, v.scorer) == ("unchecked", SIGNER, None)
    assert v.read_error == "ConnectionError"


def test_nothing_read_yet_is_unchecked(monkeypatch):
    """verdict() never reads: before the first read resolves, the honest
    answer is that the chain has not been checked."""
    configure(monkeypatch)
    v = rw.verdict()
    assert (v.status, v.signer, v.scorer) == ("unchecked", SIGNER, None)
    assert v.read_error == "not read yet"


# ── the cache ───────────────────────────────────────────────────


def aged(seconds: float, **fields) -> rw._ScorerRead:
    """A read of LEDGER that resolved `seconds` ago."""
    return rw._ScorerRead(LEDGER, time.monotonic() - seconds, **fields)


def test_a_fresh_read_is_reused_not_repeated(monkeypatch):
    configure(monkeypatch)
    chain = on_chain(monkeypatch, Chain(scorer=SIGNER))
    for _ in range(3):
        assert asyncio.run(rw.check()).status == "scorer"
    assert chain.reads == [LEDGER]


def test_a_read_past_its_ttl_is_repeated_and_picks_up_a_set_scorer(monkeypatch):
    """The TTL exists for exactly this: an admin calls set_scorer, and the
    verdict follows within one TTL without a restart."""
    configure(monkeypatch)
    monkeypatch.setattr(rw, "_last_read", aged(rw.SCORER_TTL_SECONDS + 1, scorer=OTHER))
    assert rw.verdict().status == "not_scorer"  # the stale answer, until re-read
    chain = on_chain(monkeypatch, Chain(scorer=SIGNER))
    assert asyncio.run(rw.check()).status == "scorer"
    assert chain.reads == [LEDGER]


def test_a_read_inside_its_ttl_is_trusted(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(rw, "_last_read", aged(rw.SCORER_TTL_SECONDS - 5, scorer=SIGNER))
    assert asyncio.run(rw.check()).status == "scorer"  # the fixture fails any read


def test_a_failed_read_is_retried_on_the_short_clock(monkeypatch):
    """An RPC blip at boot must not pin `unchecked` for the full TTL."""
    configure(monkeypatch)
    monkeypatch.setattr(rw, "_last_read", aged(rw.UNCHECKED_RETRY_SECONDS + 1, error="ConnectionError"))
    chain = on_chain(monkeypatch, Chain(scorer=SIGNER))
    assert asyncio.run(rw.check()).status == "scorer"
    assert chain.reads == [LEDGER]


def test_a_failed_read_is_not_hammered_inside_the_retry_window(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(rw, "_last_read", aged(rw.UNCHECKED_RETRY_SECONDS - 5, error="ConnectionError"))
    assert asyncio.run(rw.check()).status == "unchecked"  # the fixture fails any read


def test_a_new_ledger_id_is_read_at_once(monkeypatch):
    """A redeployed ledger is a new contract id; a fresh read of the old one
    says nothing about it, whatever its age."""
    configure(monkeypatch)
    monkeypatch.setattr(rw, "_last_read", rw._ScorerRead("C-OLD-LEDGER", time.monotonic(), scorer=SIGNER))
    assert rw.verdict().status == "unchecked"
    chain = on_chain(monkeypatch, Chain(scorer=OTHER))
    assert asyncio.run(rw.check()).status == "not_scorer"
    assert chain.reads == [LEDGER]


def test_concurrent_askers_share_one_read(monkeypatch):
    configure(monkeypatch)
    chain = on_chain(monkeypatch, Chain(scorer=SIGNER))

    async def _three_at_once():
        return await asyncio.gather(rw.check(), rw.check(), rw.check())

    assert [v.status for v in asyncio.run(_three_at_once())] == ["scorer"] * 3
    assert chain.reads == [LEDGER]


def test_the_ttls_are_the_documented_ones():
    """Five minutes to follow a set_scorer, thirty seconds to retry a failed
    read (docs/reputation.md). A change here is a change to that promise."""
    assert rw.SCORER_TTL_SECONDS == 300.0
    assert rw.UNCHECKED_RETRY_SECONDS == 30.0


# ── the bound ───────────────────────────────────────────────────


def test_a_read_that_hangs_times_out_to_unchecked(monkeypatch):
    """The read runs on a worker thread the loop cannot cancel; the verdict
    must resolve at the bound anyway, as unchecked, not wait for the thread."""
    configure(monkeypatch)
    monkeypatch.setattr(rw, "SCORER_READ_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()

    def _hung(ledger: str) -> str | None:
        release.wait(5)
        return SIGNER

    monkeypatch.setattr(sc, "ledger_scorer", _hung)

    async def _ask():
        started = time.monotonic()
        v = await rw.check()
        elapsed = time.monotonic() - started
        release.set()  # let the abandoned worker finish before the loop closes
        return v, elapsed

    v, elapsed = asyncio.run(_ask())
    assert (v.status, v.signer, v.scorer) == ("unchecked", SIGNER, None)
    assert v.read_error == "timed out after 0.05s"
    assert elapsed < 2


def test_the_bound_sits_above_the_clients_own_http_timeout():
    """Below 5 s it would cut off a slow read the client would have allowed."""
    assert rw.SCORER_READ_TIMEOUT_SECONDS > 5


# ── the non-blocking path the probe uses ────────────────────────


def test_verdict_never_touches_the_chain(monkeypatch):
    configure(monkeypatch)
    assert rw.verdict().status == "unchecked"  # the fixture fails any read


def test_refresh_if_stale_starts_a_read_and_does_not_wait_for_it(monkeypatch):
    configure(monkeypatch)
    chain = on_chain(monkeypatch, Chain(scorer=SIGNER))

    async def _probe_then_settle():
        rw.refresh_if_stale()
        before = rw.verdict().status  # returned without waiting
        task = rw._read_task
        assert task is not None
        await task
        return before, rw.verdict().status

    assert asyncio.run(_probe_then_settle()) == ("unchecked", "scorer")
    assert chain.reads == [LEDGER]


def test_refresh_if_stale_leaves_a_fresh_read_alone(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(rw, "_last_read", aged(1, scorer=SIGNER))

    async def _probe():
        rw.refresh_if_stale()
        return rw._read_task

    assert asyncio.run(_probe()) is None


def test_refresh_if_stale_never_reads_for_a_config_verdict(monkeypatch):
    configure(monkeypatch, key=False)

    async def _probe():
        rw.refresh_if_stale()
        return rw._read_task

    assert asyncio.run(_probe()) is None
