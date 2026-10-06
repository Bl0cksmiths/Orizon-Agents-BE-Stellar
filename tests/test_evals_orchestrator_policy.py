"""The eval's replay of the guard rule (evals/orchestrator/policy.py).

The sweep's what-ifs are only as good as this replay, so it is pinned to the
documented starting table here, and — once the guard lane's module is on the
branch — to the guard's own `decide` over a grid of scores.
"""

from __future__ import annotations

import itertools

import pytest

from evals.orchestrator.policy import STARTING, decide

CLEAN = {"injection": 0.02, "harmful": 0.02, "severity": 0.0, "real_request": 0.95, "complexity_confidence": 0.9}


def _with(**over):
    return {**CLEAN, **over}


def test_a_clean_request_is_allowed_at_its_tier():
    d = decide(CLEAN, "moderate")
    assert (d.verdict, d.tier, d.watched) == ("allow", "moderate", False)


@pytest.mark.parametrize(("score", "verdict"), [(0.40, "block"), (0.3999, "allow")])
def test_injection_blocks_at_the_line(score, verdict):
    assert decide(_with(injection=score), "low").verdict == verdict


def test_harmful_and_severity_block_with_their_own_codes():
    assert decide(_with(harmful=0.7), "low").reasons == ("harmful",)
    assert decide(_with(severity=2.0), "low").reasons == ("severity",)
    assert decide(_with(severity=1.9), "low").verdict == "allow"


def test_block_wins_over_needs_detail():
    assert decide(_with(injection=0.9, real_request=0.05), "low").verdict == "block"


@pytest.mark.parametrize(("score", "verdict"), [(0.2999, "needs_detail"), (0.30, "allow")])
def test_not_a_request_below_the_line(score, verdict):
    assert decide(_with(real_request=score), "low").verdict == verdict


def test_only_an_unsure_low_is_rounded_up_to_moderate():
    assert decide(_with(complexity_confidence=0.49), "low").tier == "moderate"
    assert decide(_with(complexity_confidence=0.05), "low").tier == "moderate"
    assert decide(_with(complexity_confidence=0.49), "moderate").tier == "moderate"
    assert decide(_with(complexity_confidence=0.49), "complex").tier == "complex"
    assert decide(_with(complexity_confidence=0.50), "low").tier == "low"


def test_the_watch_band_rests_on_the_recheck():
    watched = decide(_with(injection=0.37), "low")
    assert (watched.verdict, watched.watched) == ("allow", True)
    assert decide(_with(injection=0.37, recheck_injection=0.1), "low").verdict == "allow"
    blocked = decide(_with(injection=0.37, recheck_injection=0.35), "low")
    assert (blocked.verdict, blocked.reasons) == ("block", ("injection", "watch"))


def test_a_missing_complexity_answer_routes_to_the_strongest_tier():
    assert decide(CLEAN, None).tier == "complex"


def test_starting_values_are_the_documented_table():
    assert (
        STARTING.injection_block,
        STARTING.injection_watch,
        STARTING.harmful_block,
        STARTING.severity_block,
        STARTING.real_request_min,
        STARTING.complexity_confidence_min,
    ) == (0.40, 0.35, 0.70, 2.0, 0.30, 0.50)


def test_the_replay_agrees_with_the_guard_itself():
    guard = pytest.importorskip("app.services.intent_guard")
    policy = guard.DEFAULT_POLICY
    for name in (
        "injection_block",
        "injection_watch",
        "harmful_block",
        "severity_block",
        "real_request_min",
        "complexity_confidence_min",
    ):
        assert getattr(STARTING, name) == getattr(policy, name), name
    grid = itertools.product(
        (0.0, 0.34, 0.35, 0.69, 0.70, 1.0),  # injection
        (0.0, 0.69, 0.70),  # harmful
        (0.0, 1.0, 2.0, 3.0),  # severity
        (0.0, 0.29, 0.30, 1.0),  # real_request
        ("low", "moderate", "complex"),
        (0.49, 0.5, 0.9),  # complexity confidence
    )
    for inj, harm, sev, real, tier, conf in grid:
        scores = guard.Scores(
            injection=inj,
            harmful=harm,
            severity=sev,
            real_request=real,
            complexity=tier,
            complexity_confidence=conf,
        )
        theirs = guard.decide(scores, source="jev", model="jev", policy=policy)
        ours = decide(scores.as_dict(), tier)
        assert (ours.verdict, ours.tier, ours.watched) == (theirs.verdict, theirs.tier, theirs.watch), scores
