"""The batch deadline has to cover the reads it waits on.

The budget rule only ever checked the deadline against the PLANNING budget.
The shipped shape — 23 agents on the shared 8-thread pool, two RPC hops a
read, a 2.5 s deadline — fitted that rule with room to spare and still cut
off its last wave of reads on a healthy chain: 3 of 4 warm live reads came
back degraded. These pin the rule that the deadline covers
ceil(agents / concurrency) reads, and that reputation reads run on threads of
their own, as many as that rule assumes.

Constructed with _env_file=None so the local .env can never leak in.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services import reputation_svc as rep
from app.stellar import cache as rcache
from app.stellar import client as sc


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_the_shipped_sizing_covers_its_batch_with_room():
    s = _settings()
    waves = math.ceil(s.reputation_batch_agents / s.reputation_read_concurrency)
    assert waves * s.reputation_read_latency_seconds < s.reputation_batch_timeout_seconds
    # And the live registry fits the size the deadline was built for.
    assert s.reputation_batch_agents >= 23


def test_the_configuration_that_shipped_is_refused():
    """23 agents, 8 threads, a two-hop read at 0.9 s: three waves, 2.7 s,
    against a 2.5 s deadline. It booted then; it must not now."""
    with pytest.raises(ValidationError, match="REPUTATION_BATCH_TIMEOUT_SECONDS is shorter than the batch"):
        _settings(reputation_batch_agents=23, reputation_read_concurrency=8, reputation_read_latency_seconds=0.9)


def test_a_deadline_exactly_covering_the_batch_boots():
    s = _settings(reputation_batch_agents=32, reputation_read_concurrency=16, reputation_read_latency_seconds=1.25)
    assert s.reputation_read_latency_seconds == 1.25


@pytest.mark.parametrize("latency", [0.0, -1.0, math.nan, math.inf])
def test_a_read_latency_that_is_not_a_duration_is_refused(latency):
    with pytest.raises(ValidationError, match="REPUTATION_READ_LATENCY_SECONDS is not a finite number"):
        _settings(reputation_read_latency_seconds=latency)


@pytest.mark.parametrize("concurrency", [0, -1, 65])
def test_a_concurrency_that_is_not_a_thread_count_is_refused(concurrency):
    with pytest.raises(ValidationError, match="REPUTATION_READ_CONCURRENCY is not a whole number"):
        _settings(reputation_read_concurrency=concurrency)


def test_a_batch_of_no_agents_is_refused():
    with pytest.raises(ValidationError, match="REPUTATION_BATCH_AGENTS is not a whole number"):
        _settings(reputation_batch_agents=0)


def test_the_refusal_never_quotes_a_value():
    with pytest.raises(ValidationError) as exc:
        _settings(reputation_read_concurrency=7, reputation_batch_agents=29, reputation_read_latency_seconds=0.93)
    message = str(exc.value).split("[type=")[0]
    for value in ("7", "29", "0.93"):
        assert value not in message


# ── the threads are reserved and as many as the rule assumes ────


@pytest.fixture()
def ledger(monkeypatch):
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger="CFAKELEDGER"))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    rep.shutdown_read_pool()
    rcache.clear()
    yield
    rep.shutdown_read_pool()
    rcache.clear()


def test_reads_run_concurrency_wide_on_reserved_threads(ledger, monkeypatch):
    """Three threads, six reads. A barrier of three only opens if three reads
    are in flight together, and every read runs on a `repread` thread — not
    the default executor, where it would queue behind unrelated work."""
    monkeypatch.setattr(settings, "reputation_read_concurrency", 3)
    barrier = threading.Barrier(3, timeout=5)
    threads: set[str] = set()
    lock = threading.Lock()

    def simulate_read(_contract, _method, args, **_kw):
        with lock:
            threads.add(threading.current_thread().name)
        if args[0] in {"a0", "a1", "a2"}:
            barrier.wait()
        return {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0}

    monkeypatch.setattr(sc, "simulate_read", simulate_read)
    infos = asyncio.run(rep.fetch_reps([f"a{i}" for i in range(6)], timeout_seconds=5))

    assert not any(i.degraded for i in infos.values())
    assert len(threads) == 3
    assert all(name.startswith("repread") for name in threads)


def test_an_oversized_batch_says_so_once(ledger, monkeypatch, caplog):
    monkeypatch.setattr(settings, "reputation_batch_agents", 2)
    monkeypatch.setattr(rep, "_oversize_logged", False)
    monkeypatch.setattr(sc, "simulate_read", lambda *_a, **_k: {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0})

    with caplog.at_level(logging.WARNING, logger="app.services.reputation_svc"):
        asyncio.run(rep.fetch_reps(["a", "b", "c"]))
        asyncio.run(rep.fetch_reps(["a", "b", "c", "d"]))

    lines = [r.getMessage() for r in caplog.records if "REPUTATION_BATCH_AGENTS" in r.getMessage()]
    assert len(lines) == 1
    assert "batch of 3 agents" in lines[0]
