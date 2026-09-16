"""Reputation degradation must be LOUD and bounded.

The service used to log its prior-fallbacks at DEBUG under an INFO root
logger, so a Soroban outage produced no output at all while every agent
silently reverted to the prior — and with it the routing floor, which the
prior clears by design. These tests pin the three properties that fix:
warnings actually reach the log, they are coalesced to one line per batch
(a dozen agents polled every 15 s must not flood), and the fail-open policy
is stated explicitly rather than being an accident of the arithmetic.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.config import settings
from app.services import reputation_svc as rep
from app.stellar import cache as rcache

LOGGER_NAME = "app.services.reputation_svc"
USDC = rep.STROOPS_PER_USDC


@pytest.fixture()
def ledger_configured(monkeypatch):
    """Reputation on with a ledger id, so reads take the on-chain path."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")


def _raising(exc: Exception):
    """Stand-in for the read cache that always fails, as a hard-down RPC does."""

    async def fake(key: str, ttl_seconds: float, producer):
        raise exc

    return fake


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in _records(caplog)]


# ── warnings reach the log ──────────────────────────────────────


def test_single_read_failure_warns_with_the_agent_id(ledger_configured, monkeypatch, caplog):
    monkeypatch.setattr(rcache, "get_or_set", _raising(RuntimeError("rpc down")))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        info = asyncio.run(rep.fetch_rep("agt_01h8"))

    assert info.source == "prior"
    assert info.degraded is True
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING  # DEBUG would never be emitted
    assert "agt_01h8" in records[0].getMessage()
    assert "rpc down" in records[0].getMessage()


def test_unexpected_payload_shape_degrades_and_warns(ledger_configured, monkeypatch, caplog):
    async def wrong_shape(key: str, ttl_seconds: float, producer):
        return ["not", "a", "map"]

    monkeypatch.setattr(rcache, "get_or_set", wrong_shape)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        info = asyncio.run(rep.fetch_rep("agt_01h8"))

    assert info.degraded is True
    assert "expected a map" in _messages(caplog)[0]


def test_unconfigured_ledger_is_not_a_degradation(caplog):
    """A prior because reputation is not deployed is a cold start, not an
    outage — warning on it every poll would train operators to ignore the
    line that matters."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        infos = asyncio.run(rep.fetch_reps(["agt_01h8"]))

    assert infos["agt_01h8"].source == "prior"
    assert infos["agt_01h8"].degraded is False
    assert _records(caplog) == []


# ── one line per batch, not per agent ───────────────────────────


def test_batch_degradation_logs_once_not_per_agent(ledger_configured, monkeypatch, caplog):
    monkeypatch.setattr(rcache, "get_or_set", _raising(RuntimeError("rpc down")))
    ids = [f"agt_{i:02d}" for i in range(12)]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        infos = asyncio.run(rep.fetch_reps(ids))

    assert set(infos) == set(ids)
    assert all(i.degraded for i in infos.values())
    messages = _messages(caplog)
    assert len(messages) == 1, "12 agents must produce ONE line, not 12"
    assert "12/12 agents" in messages[0]


def test_batch_timeout_logs_once_and_marks_every_agent_degraded(ledger_configured, monkeypatch, caplog):
    async def never(key: str, ttl_seconds: float, producer):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(rcache, "get_or_set", never)
    ids = ["agt_a", "agt_b", "agt_c"]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        infos = asyncio.run(rep.fetch_reps(ids, timeout_seconds=0.02))

    assert all(i.degraded for i in infos.values())
    messages = _messages(caplog)
    assert len(messages) == 1
    assert "batch read aborted" in messages[0]
    assert "3/3 agents" in messages[0]


def test_partial_batch_names_only_the_failed_agents(ledger_configured, monkeypatch, caplog):
    async def flaky(key: str, ttl_seconds: float, producer):
        if key.endswith("bad"):
            raise RuntimeError("rpc down")
        return {"sum_w": 9000 * 10 * USDC, "weight": 10 * USDC, "count": 4, "disputed": 0}

    monkeypatch.setattr(rcache, "get_or_set", flaky)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        infos = asyncio.run(rep.fetch_reps(["agt_good", "agt_bad"]))

    assert infos["agt_good"].source == "onchain"
    assert infos["agt_good"].degraded is False
    assert infos["agt_bad"].degraded is True
    messages = _messages(caplog)
    assert len(messages) == 1
    assert "1/2 agents" in messages[0]
    assert "agt_bad" in messages[0]
    assert "agt_good" not in messages[0]


def test_cold_start_and_failed_read_are_told_apart_only_by_degraded(ledger_configured, monkeypatch, caplog):
    """The pair that genuinely looks alike: a never-rated agent and an agent
    whose read failed BOTH report source="prior", so `degraded` is the only
    thing separating "nobody has rated this one yet" from "we could not ask".
    Comparing a degraded read against a RATED agent instead — onchain vs
    prior — is exactly what hid the fail-open behaviour originally, because
    the two states that collide were never put side by side. If this stops
    holding, an operator reading the dashboard sees a newcomer and an
    unreachable ledger as the same thing, and the outage warning starts
    naming cold starts: a permissionless newcomer would page whoever is
    on call, which trains them to ignore the line that matters.
    """

    async def flaky(key: str, ttl_seconds: float, producer):
        if key.endswith("unread"):
            raise RuntimeError("rpc down")
        # A READABLE ledger holding no evidence — the real cold-start path
        # (_info_from_state returns _prior_info when count and weight are 0),
        # not a missing entry, which would take the failure branch instead.
        return {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0}

    monkeypatch.setattr(rcache, "get_or_set", flaky)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        infos = asyncio.run(rep.fetch_reps(["agt_cold", "agt_unread"]))

    cold, unread = infos["agt_cold"], infos["agt_unread"]
    assert cold.source == "prior"
    assert unread.source == "prior"
    assert cold.degraded is False
    assert unread.degraded is True
    # And nothing else tells them apart: identical prior score, identical
    # routing verdict. `degraded` carries the whole distinction.
    assert cold.model_dump(exclude={"agent_id", "degraded"}) == unread.model_dump(exclude={"agent_id", "degraded"})
    assert rep.passes_floor(cold) == rep.passes_floor(unread)

    messages = _messages(caplog)
    assert len(messages) == 1
    assert "1/2 agents" in messages[0]
    assert "agt_unread" in messages[0]
    assert "agt_cold" not in messages[0], "a cold start is not an outage and must never be named as one"


def test_degradation_warning_caps_the_named_agents(ledger_configured, monkeypatch, caplog):
    """The batch is the whole registry; the line still has to be readable."""
    monkeypatch.setattr(rcache, "get_or_set", _raising(RuntimeError("rpc down")))
    extra = 5
    ids = [f"agt_{i:03d}" for i in range(rep._DEGRADED_LOG_AGENT_LIMIT + extra)]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(rep.fetch_reps(ids))

    message = _messages(caplog)[0]
    assert f"+{extra} more" in message
    assert ids[-1] not in message


def test_thirty_agent_outage_emits_exactly_one_warning_record(ledger_configured, monkeypatch, caplog):
    """Thirty agents is the registry the dashboard polls every 15 s, and the
    coalescing has to hold at that size: ONE record, not one per agent.
    Records rather than distinct messages: per-agent warnings would render
    identically, so counting unique text would report "one warning" while
    thirty lines actually went out per poll — two a second for the length of
    the outage, burying every other line and burning Render's log retention.
    """
    monkeypatch.setattr(rcache, "get_or_set", _raising(RuntimeError("rpc down")))
    ids = [f"agt_{i:03d}" for i in range(30)]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        infos = asyncio.run(rep.fetch_reps(ids))

    assert set(infos) == set(ids)
    assert all(i.degraded for i in infos.values())
    warnings = [r for r in _records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "30 agents must produce ONE warning record, not 30"
    assert len(_records(caplog)) == 1, "and nothing else on the way through"
    message = warnings[0].getMessage()
    assert "30/30 agents" in message
    assert f"+{len(ids) - rep._DEGRADED_LOG_AGENT_LIMIT} more" in message


# ── the fail-open policy is explicit ────────────────────────────


def test_default_config_fails_open_and_says_so(ledger_configured, monkeypatch, caplog):
    monkeypatch.setattr(rcache, "get_or_set", _raising(RuntimeError("rpc down")))
    assert rep.prior_clears_floor() is True

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(rep.fetch_reps(["agt_01h8"]))

    assert "failing OPEN" in _messages(caplog)[0]


def test_floor_above_the_prior_fails_closed_and_says_so(ledger_configured, monkeypatch, caplog):
    monkeypatch.setattr(settings, "reputation_floor_bps", 9000)
    monkeypatch.setattr(rcache, "get_or_set", _raising(RuntimeError("rpc down")))
    assert rep.prior_clears_floor() is False

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(rep.fetch_reps(["agt_01h8"]))

    assert "failing CLOSED" in _messages(caplog)[0]


def test_degraded_prior_stays_routable_under_the_default_floor():
    """The kept policy, asserted rather than implied: an outage returns every
    agent to the prior, which clears the default floor, so routing carries on
    (loudly logged) instead of the network going dark."""
    info = rep._prior_info("agt_01h8", degraded=True)
    assert info.degraded is True
    assert rep.passes_floor(info) is True


def test_configured_floor_still_governs_degraded_agents(monkeypatch):
    """passes_floor does not special-case degraded infos — an operator who
    raises the floor above the prior bound gets a floor that fails closed."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 9000)
    assert rep.passes_floor(rep._prior_info("agt_01h8", degraded=True)) is False


def test_onchain_reads_are_never_marked_degraded():
    info = rep._info_from_state("agt_04m1", {"sum_w": 9000 * 10 * USDC, "weight": 10 * USDC, "count": 4, "disputed": 0})
    assert info.source == "onchain"
    assert info.degraded is False
