"""Unit tests for the reputation smoothing service."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import reputation_svc as rep
from app.stellar import cache as rcache
from app.stellar import client as sc

USDC = rep.STROOPS_PER_USDC


def _sum_w(mean_bps: int, weight: int) -> int:
    """Build a sum_w accumulator that yields the given raw mean."""
    return mean_bps * weight


def test_smoothed_is_prior_with_no_evidence():
    assert rep.smoothed_bps(0, 0) == settings.reputation_prior_bps


def test_smoothed_moves_toward_evidence_as_weight_grows():
    prior = settings.reputation_prior_bps
    light = rep.smoothed_bps(_sum_w(9500, 1 * USDC), 1 * USDC)
    heavy = rep.smoothed_bps(_sum_w(9500, 100 * USDC), 100 * USDC)
    assert prior < light < heavy < 9500 or heavy == 9500
    # Heavy evidence should sit within 5% of the raw mean.
    assert heavy > 9000


def test_heavy_negative_evidence_sinks_below_floor():
    # 20 USDC of settled work rated 10/100 must not survive the floor.
    weight = 20 * USDC
    smoothed = rep.smoothed_bps(_sum_w(1000, weight), weight)
    lb = rep.lower_bound_bps(smoothed, weight)
    assert lb < settings.reputation_floor_bps


def test_lower_bound_below_mean_and_tightens_with_evidence():
    smoothed = rep.smoothed_bps(_sum_w(8000, 5 * USDC), 5 * USDC)
    lb_small = rep.lower_bound_bps(smoothed, 5 * USDC)
    lb_big = rep.lower_bound_bps(smoothed, 500 * USDC)
    assert lb_small < smoothed
    assert lb_small < lb_big <= smoothed


def test_prior_only_agent_passes_default_floor():
    # Cold start: a brand-new agent must be routable.
    info = rep._prior_info("agt_new")
    assert info.source == "prior"
    assert rep.passes_floor(info)


def test_passes_floor_none_is_permissive():
    assert rep.passes_floor(None)


def test_synthetic_rating_baked_artifact():
    rating, weight = rep.synthetic_rating({"source": "baked"}, 0.054)
    assert rating == 95
    assert weight == round(0.054 * USDC)


def test_synthetic_rating_failed_step():
    rating, weight = rep.synthetic_rating(None, 0.18)
    assert rating == 20
    assert weight == round(0.18 * USDC)


def test_synthetic_rating_clean_artifact_beats_violations():
    clean, _ = rep.synthetic_rating({"artifact": {"title": "x"}, "critic_violations": []}, 0.05)
    dirty, _ = rep.synthetic_rating({"artifact": {"title": "x"}, "critic_violations": ["a", "b", "c"]}, 0.05)
    assert clean == 95
    assert dirty < clean
    assert 0 <= dirty <= 100


def test_rating_weight_capped_and_floored():
    cap = rep.max_rating_weight_usdc()
    assert rep.rating_weight_stroops(cap * 10) == round(cap * USDC)
    assert rep.rating_weight_stroops(0.0) == 1


# ── one rating can at most equal the prior ──────────────────────


def test_the_cap_that_binds_is_the_prior_weight():
    """Shipped numbers: 1.0 x the 12 USDC prior, inside the 100 USDC bound."""
    assert rep.max_rating_weight_usdc() == settings.reputation_prior_weight_usdc == 12.0
    assert rep.rating_weight_stroops(100.0) == 12 * USDC


@pytest.mark.parametrize("rating", [95, 20, 100, 0])
def test_one_whale_rating_moves_a_newcomer_at_most_halfway(rating):
    """The audit's self-dealt run: one rating on a step priced at the 100 USDC
    ceiling. It used to set the score — 9232 for a 95 — because its 100 USDC
    of weight outweighed the 12 USDC prior eight times over. Capped at the
    prior, it lands exactly halfway between the prior and itself, and no
    single rating can get further than that."""
    weight = rep.rating_weight_stroops(100.0)
    info = rep._info_from_state("agt_whale", {"sum_w": rating * 100 * weight, "weight": weight, "count": 1})
    prior = settings.reputation_prior_bps
    assert info.smoothed_bps == (prior + rating * 100) // 2
    assert abs(info.smoothed_bps - prior) <= abs(rating * 100 - prior) / 2


def test_one_95_on_a_whale_job_no_longer_owns_the_bound():
    weight = rep.rating_weight_stroops(100.0)
    info = rep._info_from_state("agt_whale", {"sum_w": 9500 * weight, "weight": weight, "count": 1})
    assert info.smoothed_bps == 8250
    assert info.lower_bound_bps < 8980, "the uncapped whale's bound"


def test_the_ratio_is_one_number_that_moves_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "reputation_max_rating_to_prior_ratio", 0.25)
    assert rep.max_rating_weight_usdc() == 3.0
    assert rep.rating_weight_stroops(100.0) == 3 * USDC
    # And the absolute cap still bounds a large ratio.
    monkeypatch.setattr(settings, "reputation_max_rating_to_prior_ratio", 50.0)
    assert rep.max_rating_weight_usdc() == settings.reputation_max_rating_weight_usdc


def _no_rpc(*_args, **_kwargs):  # pragma: no cover - must never run
    raise AssertionError("an unconfigured ledger must not be read")


def test_fetch_rep_prior_fallback_without_contract(monkeypatch):
    # No reputation ledger id → prior path, no RPC call. Set here rather than
    # inherited from conftest's blanking, and the RPC is a trap, so this can
    # only pass on the disabled path it names.
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "")
    monkeypatch.setattr(sc, "simulate_read", _no_rpc)
    info = asyncio.run(rep.fetch_rep("agt_01h8"))
    assert info.source == "prior"
    assert info.degraded is False
    assert info.smoothed_bps == settings.reputation_prior_bps
    assert info.count == 0


def _fake_ledger(monkeypatch, chain: dict[str, dict[str, int]]) -> None:
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger="CFAKELEDGER"))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    monkeypatch.setattr(sc, "simulate_read", lambda _c, _m, args, **_k: chain[args[0]])
    rcache.clear()


def test_fetch_rep_reads_the_configured_ledger(monkeypatch):
    """The on-chain path these tests used to skip because conftest blanks the
    ledger id: with a ledger configured the numbers come from rep_state."""
    weight = 10 * USDC
    _fake_ledger(monkeypatch, {"agt_01h8": {"sum_w": _sum_w(9000, weight), "weight": weight, "count": 4}})
    try:
        info = asyncio.run(rep.fetch_rep("agt_01h8"))
    finally:
        rcache.clear()
    assert info.source == "onchain"
    assert info.avg_bps == 9000
    assert info.count == 4


def test_fetch_reps_returns_each_agent_its_own_entry(monkeypatch):
    """One entry per agent, each read from ITS rep_state — distinct numbers
    per agent, so a batch that crossed or dropped reads cannot pass."""
    ids = ["agt_01h8", "agt_02k2", "agt_03d9"]
    weight = 10 * USDC
    chain = {a: {"sum_w": _sum_w(6000 + 1000 * i, weight), "weight": weight, "count": i + 1} for i, a in enumerate(ids)}
    _fake_ledger(monkeypatch, chain)
    try:
        infos = asyncio.run(rep.fetch_reps(ids))
    finally:
        rcache.clear()
    assert set(infos) == set(ids)
    for i, agent_id in enumerate(ids):
        assert infos[agent_id].agent_id == agent_id
        assert infos[agent_id].source == "onchain"
        assert infos[agent_id].avg_bps == 6000 + 1000 * i
        assert infos[agent_id].count == i + 1


def test_info_from_state_zero_evidence_is_prior():
    info = rep._info_from_state("agt_new", {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0})
    assert info.source == "prior"
    assert info.smoothed_bps == settings.reputation_prior_bps


def test_info_from_state_math():
    weight = 10 * USDC
    info = rep._info_from_state(
        "agt_04m1",
        {"sum_w": _sum_w(9000, weight), "weight": weight, "count": 4, "disputed": 1},
    )
    assert info.source == "onchain"
    assert info.avg_bps == 9000
    assert settings.reputation_prior_bps < info.smoothed_bps < 9000
    assert info.dispute_rate_bps == 2500


# ── ledger state the scorer must refuse ─────────────────────────


@pytest.mark.parametrize(
    "state",
    [
        # A negative weight scored the MAXIMUM: smoothed and bound both 10000.
        {"sum_w": -(10**9), "weight": -(10**8), "count": 1, "disputed": 0},
        {"sum_w": 0, "weight": -12 * USDC, "count": 1, "disputed": 0},
        # sum_w above 10000 x weight: an average of 10^12 bps.
        {"sum_w": 10**12, "weight": 1, "count": 1, "disputed": 0},
        {"sum_w": 1, "weight": 0, "count": 5, "disputed": 0},
        {"sum_w": -(10**12), "weight": USDC, "count": 1, "disputed": 0},
        {"sum_w": 0, "weight": 0, "count": -1, "disputed": 0},
        {"sum_w": 0, "weight": 0, "count": 1, "disputed": -1},
        # Not integers at all: NaN and inf must never reach the arithmetic.
        {"sum_w": float("nan"), "weight": USDC, "count": 1, "disputed": 0},
        {"sum_w": 9000 * USDC, "weight": float("inf"), "count": 1, "disputed": 0},
        {"sum_w": True, "weight": USDC, "count": 1, "disputed": 0},
        {"sum_w": "9000", "weight": USDC, "count": 1, "disputed": 0},
    ],
)
def test_out_of_range_state_is_refused_not_scored(state):
    with pytest.raises((ValueError, TypeError)):
        rep._info_from_state("agt_x", state)


def test_out_of_range_state_reads_as_a_failed_read(monkeypatch):
    """Through the real read: refused state degrades to the prior, it does not
    score — and a negative weight in particular no longer scores 10000."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger="CFAKELEDGER"))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    monkeypatch.setattr(
        sc, "simulate_read", lambda *_a, **_k: {"sum_w": -(10**9), "weight": -(10**8), "count": 1, "disputed": 0}
    )
    rcache.clear()
    try:
        info = asyncio.run(rep.fetch_reps(["agt_x"]))["agt_x"]
    finally:
        rcache.clear()

    assert info.degraded is True
    assert info.source == "prior"
    assert info.lower_bound_bps == rep._prior_info("agt_x").lower_bound_bps


def test_zero_weight_with_ratings_is_the_prior_mean_not_a_failure():
    """Fully decayed evidence: ratings were counted, their weight has gone.
    Readable and in range, so it scores — at the prior mean, average 0."""
    info = rep._info_from_state("agt_x", {"sum_w": 0, "weight": 0, "count": 5, "disputed": 0})
    assert info.degraded is False
    assert info.smoothed_bps == settings.reputation_prior_bps
    assert info.avg_bps == 0


def test_a_full_scale_mean_is_in_range():
    """The edge of the check is inclusive: every rating at 100/100."""
    info = rep._info_from_state("agt_x", {"sum_w": 10_000 * USDC, "weight": USDC, "count": 1, "disputed": 0})
    assert info.avg_bps == 10_000


# ── the scoring helpers at their edges (audit mutants M1, M2, M15) ─


def test_a_negative_weight_adds_no_confidence_to_the_bound():
    """M1: `lower_bound_bps` clamps weight at 0. Unclamped, a negative weight
    SUBTRACTS from the prior's sample size and widens the bound."""
    assert rep.lower_bound_bps(7000, -6 * USDC) == rep.lower_bound_bps(7000, 0)


def test_no_mass_at_all_smooths_to_the_prior():
    """M2: prior mass plus evidence at or below zero has no mean to take."""
    assert rep.smoothed_bps(0, -rep.prior_weight_stroops()) == settings.reputation_prior_bps
    assert rep.smoothed_bps(5_000 * USDC, -rep.prior_weight_stroops() - USDC) == settings.reputation_prior_bps


def test_smoothed_is_clamped_to_the_scale():
    """M15: the mean is clamped into 0..10000 whatever the accumulator says."""
    assert rep.smoothed_bps(10**18, 1) == 10_000
    assert rep.smoothed_bps(-(10**18), 1) == 0


# ── the prior's bound follows the config (audit mutant M8) ──────


@pytest.mark.parametrize(
    ("prior_bps", "prior_weight_usdc", "expected"),
    [
        # p - sqrt(p(1-p)/n), worked by hand rather than by the helper under
        # test: 0.70 - sqrt(0.21/12) = 0.5677; 0.80 - sqrt(0.16/12) = 0.6845;
        # 0.70 - sqrt(0.21/48) = 0.6339.
        (7000, 12.0, 5677),
        (8000, 12.0, 6845),
        (7000, 48.0, 6339),
    ],
)
def test_the_prior_bound_is_computed_from_the_config(monkeypatch, prior_bps, prior_weight_usdc, expected):
    monkeypatch.setattr(settings, "reputation_prior_bps", prior_bps)
    monkeypatch.setattr(settings, "reputation_prior_weight_usdc", prior_weight_usdc)
    info = rep._prior_info("agt_new")
    assert info.smoothed_bps == prior_bps
    assert info.lower_bound_bps == expected
    assert rep.cold_start_margin().lower_bound_bps == expected
