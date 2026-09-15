"""What a rating actually costs — the two holes ADR 0005 closes, in numbers.

Story 2.03's premise is that non-delivery costs the agent. Two things made
that false, and both are arithmetic rather than opinion, so every assertion
here pins the real value the shipped config produces rather than a direction
of travel:

  - D3: `synthetic_rating` returned 20 only for FALSY output, so any non-empty
    dict reached base 70 — which IS `reputation_prior_bps`. Junk therefore held
    the mean at the prior while growing the evidence mass, and the Wilson lower
    bound ROSE with every response: 5677 → 5746 over 25 of them. Answering
    `{"ok": true}` forever outscored failing honestly, and the routing floor
    could never exclude it.
  - D4: `rating_weight_stroops` floors weight at 1 stroop and on-chain evidence
    decays 7.5% per week, so below a certain price no volume of failure can
    outrun decay. `registry_sync` now refuses that price at ingress.

The first-party half of D3 is pinned here too, exhaustively: local workers
legitimately return text with no artifact, regrading them is out of scope, and
a regression there would be a silent economic change to every seeded agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services import reputation_svc as rep

USDC = rep.STROOPS_PER_USDC
FLOOR = settings.reputation_floor_bps

# The lower bound a prior-only agent carries — the number every claim below is
# measured against, since base 70 scores exactly to it.
PRIOR_LOWER_BOUND = 5677

# 0.054 USDC is code.gen's catalog price and the one ADR 0005 quotes its
# figures at, so the 5677 → 5746 exploit reproduces here digit for digit.
PRICE = 0.054


def _rep_info(n: int, price: float, rating: int) -> rep.RepInfo:
    """The RepInfo an agent carries after `n` identical on-chain ratings.

    Mirrors what ReputationLedger accumulates: `weight` is the summed evidence
    mass in stroops and `sum_w` is the rating in bps times that weight, which
    is the pair `smoothed_bps`/`lower_bound_bps` are defined over.
    """
    weight = n * rep.rating_weight_stroops(price)
    smoothed = rep.smoothed_bps(weight * rating * 100, weight)
    return rep.RepInfo(
        agent_id="ext_probe",
        smoothed_bps=smoothed,
        lower_bound_bps=rep.lower_bound_bps(smoothed, weight),
        avg_bps=rating * 100 if n else 0,
        count=n,
        weight=weight,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain" if n else "prior",
    )


# ── the config every number below depends on ────────────────────


def test_the_shipped_config_is_what_these_numbers_assume():
    # Pinned so a config change surfaces as this failing, not as a dozen
    # unexplained off-by-a-few assertions further down.
    assert settings.reputation_prior_bps == 7000
    assert settings.reputation_prior_weight_usdc == 12.0
    assert FLOOR == 5500
    assert rep.WILSON_Z == 1.0
    assert rep.lower_bound_bps(settings.reputation_prior_bps, 0) == PRIOR_LOWER_BOUND
    assert rep.prior_clears_floor()  # a newcomer is routable; the floor bites below the prior


# ── D3: junk must not outscore honest failure ───────────────────


def test_the_exploit_is_exactly_the_base_score_equalling_the_prior():
    """Why junk paid: 70/100 IS the prior, so the mean never moved.

    Kept as the contrast the fix is measured against — and asserted on the
    FIRST-PARTY path, which is deliberately unchanged, so this documents the
    arithmetic rather than a surviving hole. Evidence mass grows, the mean
    holds at 7000, and the Wilson bound tightens UPWARD toward it.
    """
    rating, weight = rep.synthetic_rating({"ok": True}, PRICE)
    assert rating == 70 == settings.reputation_prior_bps // 100
    assert weight == 540_000

    assert _rep_info(0, PRICE, 70).lower_bound_bps == PRIOR_LOWER_BOUND
    assert _rep_info(1, PRICE, 70).lower_bound_bps == 5680
    assert _rep_info(25, PRICE, 70).lower_bound_bps == 5746
    assert rep.passes_floor(_rep_info(25, PRICE, 70))


@pytest.mark.parametrize(
    "junk",
    [
        {"ok": True},
        {"status": "accepted"},
        {"message": "done", "elapsed_ms": 4},
        {"summary": "handled it"},  # prose with nothing behind it
        {"artifact": None},  # the key, with no artifact under it
        {"critic_violations": "none"},  # a string is not critic content
        {"source": "baked"},  # 95 is above base, so the gate covers it too
    ],
)
def test_untrusted_output_with_nothing_checkable_scores_as_non_delivery(junk: dict[str, Any]):
    rating, weight = rep.synthetic_rating(junk, PRICE, first_party=False)
    assert rating == 20  # the same 20 a timeout earns — the buyer got the same thing
    assert weight == 540_000  # the weight is untouched; only the score moved


def test_untrusted_junk_never_raises_the_lower_bound():
    """The hole was that evidence ACCUMULATED without moving the mean.

    Now the first junk response already sits below the prior bound, and the
    bound only ever falls from there.
    """
    assert _rep_info(1, PRICE, 20).lower_bound_bps == 5654 < PRIOR_LOWER_BOUND
    assert _rep_info(25, PRICE, 20).lower_bound_bps == 5188

    bounds = [_rep_info(n, PRICE, 20).lower_bound_bps for n in range(26)]
    assert bounds == sorted(bounds, reverse=True)  # never rises, at any volume


def test_repeated_untrusted_junk_crosses_the_routing_floor():
    # Nine junk responses at 0.054 USDC — the same count ADR 0005 records for
    # nine honest failures at that price, because they now score identically.
    assert _rep_info(8, PRICE, 20).lower_bound_bps == 5506
    assert rep.passes_floor(_rep_info(8, PRICE, 20))
    assert _rep_info(9, PRICE, 20).lower_bound_bps == 5485
    assert not rep.passes_floor(_rep_info(9, PRICE, 20))


@pytest.mark.parametrize(
    ("delivered", "expected"),
    [
        ({"artifact": {"title": "x"}}, 85),
        ({"artifact": {"title": "x"}, "critic_violations": []}, 95),
        ({"artifact": {"title": "x"}, "critic_violations": ["a", "b", "c"]}, 76),
        ({"critic_violations": []}, 80),  # critic content with no artifact still counts
        ({"validator_violations": ["a"]}, 67),
    ],
)
def test_untrusted_output_that_delivers_still_earns_the_full_scale(delivered: dict[str, Any], expected: int):
    # The gate is a delivery check, NOT a quality grader: an external agent
    # that ships something is scored on exactly the same scale as a local one.
    assert rep.synthetic_rating(delivered, PRICE, first_party=False)[0] == expected
    assert rep.synthetic_rating(delivered, PRICE)[0] == expected


# ── D3: first-party scoring is untouched ────────────────────────


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (None, 20),  # timed out or raised
        ({}, 20),  # empty dict is falsy — the pre-existing non-delivery branch
        ({"summary": "text only, no artifact"}, 70),  # a legitimate local answer
        ({"ok": True}, 70),  # junk from a LOCAL worker is still base, by design
        ({"artifact": {"title": "x"}}, 85),
        ({"artifact": {"title": "x"}, "critic_violations": []}, 95),
        ({"artifact": {"title": "x"}, "critic_violations": ["a", "b", "c"]}, 76),
        ({"critic_violations": []}, 80),
        ({"critic_violations": ["x"] * 20}, 40),  # the penalty caps at ten violations
        ({"validator_violations": ["a"]}, 67),  # the legacy key still reads
        ({"source": "baked"}, 95),  # deterministic pre-validated kit output
    ],
)
def test_first_party_scoring_is_byte_identical(output: dict[str, Any] | None, expected: int):
    # The regression wall: every seeded agent is scored through this path, so a
    # change here is a silent repricing of the whole catalog's reputation.
    assert rep.synthetic_rating(output, PRICE)[0] == expected
    assert rep.synthetic_rating(output, PRICE, first_party=True)[0] == expected


def test_the_default_is_first_party_and_the_flag_is_keyword_only():
    # The settler opts a step INTO the stricter scoring; a caller that knows
    # nothing about the flag keeps the behaviour it had. And the two existing
    # positional arguments keep their meaning — the flag cannot be passed by
    # position, so no call site can drift into it.
    assert rep.synthetic_rating({"ok": True}, PRICE) == rep.synthetic_rating({"ok": True}, PRICE, first_party=True)
    with pytest.raises(TypeError):
        rep.synthetic_rating({"ok": True}, PRICE, False)


@pytest.mark.parametrize("price", [0.0, 0.007, 0.054, 0.18, 1_000.0])
def test_the_weight_is_the_same_whichever_side_of_the_boundary_scored_it(price: float):
    # Only the SCORE is trust-sensitive. Weight is the step's quoted price and
    # is identical for both, including at the 1-stroop floor and the cap.
    expected = rep.rating_weight_stroops(price)
    assert rep.synthetic_rating({"artifact": {"t": 1}}, price)[1] == expected
    assert rep.synthetic_rating({"artifact": {"t": 1}}, price, first_party=False)[1] == expected
    assert rep.synthetic_rating(None, price, first_party=False)[1] == expected
