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

import asyncio
import logging
from typing import Any

import pytest
from stellar_sdk import scval

from app.config import settings
from app.services import registry_sync
from app.services import reputation_svc as rep
from app.state import state

USDC = rep.STROOPS_PER_USDC
FLOOR = settings.reputation_floor_bps

# The lower bound a prior-only agent carries — the number every claim below is
# measured against, since base 70 scores exactly to it.
PRIOR_LOWER_BOUND = 5677

# 0.054 USDC is code.gen's catalog price and the one ADR 0005 quotes its
# figures at, so the 5677 → 5746 exploit reproduces here digit for digit.
PRICE = 0.054

# Decayed evidence mass at 20/100 that puts the lower bound under the floor.
# Everything in the D4 section is this number divided by a rating weight.
EVIDENCE_TO_CROSS_STROOPS = 4_481_328

SYNC_LOGGER = "app.services.registry_sync"
REGISTRY_ID = "CFAKEREGISTRY"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
DUST = "ext_dust"


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
    ],
)
def test_untrusted_output_that_delivers_still_earns_the_full_scale(delivered: dict[str, Any], expected: int):
    # The gate is a delivery check, NOT a quality grader: an external agent
    # that ships something is scored on exactly the same scale as a local one.
    assert rep.synthetic_rating(delivered, PRICE, first_party=False)[0] == expected
    assert rep.synthetic_rating(delivered, PRICE)[0] == expected


def test_validator_violations_is_the_one_place_the_two_scales_diverge():
    """The only shape scored differently for an untrusted agent, and the
    divergence is deliberate.

    `synthetic_rating` still honours `validator_violations` for a FIRST-PARTY
    worker, which can genuinely set it. An untrusted agent cannot: the response
    contract drops the key before rating ever sees it, because it is not on the
    allowlist. Counting it as checkable work therefore scored a shape that can
    never arrive — and an operator who guessed that name had it silently
    discarded AND was then rated as having delivered nothing.
    """
    sent = {"validator_violations": ["a"]}
    assert rep.synthetic_rating(sent, PRICE)[0] == 67  # first-party: unchanged
    assert rep.synthetic_rating(sent, PRICE, first_party=False)[0] == 20  # untrusted: non-delivery

    # and the reason it can never arrive
    from app.agents.workers.external_contract import parse_operator_output

    assert "validator_violations" not in parse_operator_output({"summary": "x", **sent})


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


# ── D4: a price too small for the floor to reach ────────────────


def _raw(agent_id: str, **overrides: Any) -> dict[str, Any]:
    """A registry `get` record as simulate_read decodes it — 0.05 USDC."""
    record: dict[str, Any] = {
        "active": True,
        "id": agent_id,
        "name": f"{agent_id}.worker",
        "owner": OWNER,
        "price": 500_000,
        "registered_at": 1_757_000_000,
        "skills": ["translate", "en"],
    }
    record.update(overrides)
    return record


def _sync_log(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == SYNC_LOGGER and r.levelno == level]


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """A configured registry contract backed by an in-memory record set.

    Restores `state.agents` and the module's once-per-process log guards, so
    this file's refusals never leak into another test's log assertions.
    """
    records: dict[str, dict[str, Any]] = {}
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)

    def fake_simulate_read(contract_id: str, fn: str, args: list | None = None, source: str | None = None) -> Any:
        if fn == "list_ids":
            return list(records)
        assert fn == "get"
        return records[scval.from_symbol(args[0])]  # asserts args arrive as real syms

    monkeypatch.setattr(registry_sync.sc, "simulate_read", fake_simulate_read)
    agents_before = dict(state.agents)
    yield records
    state.agents.clear()
    state.agents.update(agents_before)
    registry_sync._refused_price_ids.clear()
    registry_sync._skipped_agt_ids.clear()
    registry_sync._disabled_logged = False


@pytest.mark.parametrize(
    ("stroops", "why"),
    [
        (1, "the 1-stroop weight floor itself — no price is worse than this"),
        (1_000, "0.0001 USDC: 4,482 failures, 336 a week forever"),
        (9_999, "one stroop under the minimum"),
        (0, "the contract does not require a positive price (#44)"),
        (-500_000, "…nor a non-negative one (#44)"),
    ],
)
def test_the_mapper_refuses_a_price_the_floor_could_never_reach(stroops: int, why: str):
    # Zero and negative stay refused: the floor is positive, so it SUBSUMES
    # #44's sign check rather than replacing or duplicating it.
    with pytest.raises(registry_sync.UnbelievablePrice):
        registry_sync._to_agent(_raw(DUST, price=stroops))


def test_the_minimum_itself_is_believable():
    assert registry_sync.MIN_ONCHAIN_PRICE_USDC == 0.001
    agent = registry_sync._to_agent(_raw(DUST, price=10_000))
    assert agent.price == pytest.approx(registry_sync.MIN_ONCHAIN_PRICE_USDC)
    # And the ceiling is untouched by the new bound.
    assert registry_sync._to_agent(_raw(DUST, price=int(settings.max_charge_usdc * 1e7)))


def test_a_reprice_into_dust_delists_and_the_refusal_coalesces(registry, caplog):
    caplog.set_level(logging.DEBUG, logger=SYNC_LOGGER)
    registry[DUST] = _raw(DUST)
    assert asyncio.run(registry_sync.sync_once()) == 1
    assert state.agents[DUST].price == pytest.approx(0.05)

    # Register believably, wait to be indexed, then reprice into dust on-chain.
    # Known ids are re-read every pass, so this must EVICT the indexed copy —
    # leaving the old price standing would be the stale mirror the re-read
    # exists to prevent, and would leave a dust-priced agent routable.
    registry[DUST] = _raw(DUST, price=1)
    for _ in range(4):  # a 15s loop against a standing refusal must not flood
        assert asyncio.run(registry_sync.sync_once()) == 0

    assert DUST not in state.agents
    warnings = _sync_log(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert DUST in warnings[0].getMessage()
    assert "delisted" in warnings[0].getMessage()
    assert len(_sync_log(caplog, logging.DEBUG)) == 3


def test_one_dust_record_never_kills_the_pass(registry, caplog):
    caplog.set_level(logging.DEBUG, logger=SYNC_LOGGER)
    registry.update({DUST: _raw(DUST, price=1), "ext_ok": _raw("ext_ok")})

    assert asyncio.run(registry_sync.sync_once()) == 1
    assert DUST not in state.agents
    assert state.agents["ext_ok"].price == pytest.approx(0.05)


# ── D4: the minimum restores the mechanism ──────────────────────


def _steady_state_weight(failures_per_week: int, price: float) -> int:
    """Decayed evidence mass an agent settles at, failing at a constant rate.

    ReputationLedger retains DECAY_BPS_PER_EPOCH of the accumulated mass each
    epoch, so a constant intake `w` per epoch converges on w / (1 - retention).
    This is the number that decides whether decay outruns accumulation.
    """
    retention = rep.DECAY_BPS_PER_EPOCH / 10_000
    return int(failures_per_week * rep.rating_weight_stroops(price) / (1 - retention))


def _excluded_at_rate(failures_per_week: int, price: float) -> bool:
    """Whether a constant failure rate holds the agent BELOW the routing floor.

    Not "reaches it once": the equilibrium mass is what survives decay, so this
    is the honest form of the question "can this agent ever be excluded".
    """
    weight = _steady_state_weight(failures_per_week, price)
    smoothed = rep.smoothed_bps(weight * 2000, weight)  # every rating a 20/100
    return rep.lower_bound_bps(smoothed, weight) < FLOOR


def test_an_agent_at_the_minimum_can_still_be_excluded():
    """The point of the minimum: at it, failing still costs the agent routing."""
    price = registry_sync.MIN_ONCHAIN_PRICE_USDC
    assert rep.rating_weight_stroops(price) == 10_000
    assert -(-EVIDENCE_TO_CROSS_STROOPS // 10_000) == 449  # failures needed

    assert _rep_info(448, price, 20).lower_bound_bps == 5500
    assert rep.passes_floor(_rep_info(448, price, 20))
    assert _rep_info(449, price, 20).lower_bound_bps == 5499
    assert not rep.passes_floor(_rep_info(449, price, 20))


def test_decay_does_not_outrun_accumulation_at_the_minimum():
    # 7.5% of the evidence evaporates every week, so a one-off burst is not
    # enough — the agent has to be held below the floor by ongoing failure.
    assert rep.DECAY_BPS_PER_EPOCH == 9_250
    assert rep.EPOCH_SECONDS == 604_800

    price = registry_sync.MIN_ONCHAIN_PRICE_USDC
    assert not _excluded_at_rate(33, price)
    assert _excluded_at_rate(34, price)  # ~5 failures a day holds it out, forever


def test_below_the_minimum_the_floor_is_unreachable():
    # 1 stroop: rating_weight_stroops floors here, so nothing prices worse.
    dust = 0.0000001
    assert rep.rating_weight_stroops(dust) == 1

    assert not _excluded_at_rate(336_099, dust)
    assert _excluded_at_rate(336_100, dust)
    # 336,100 failures a week is 2,000 an hour, sustained forever, before the
    # routing floor applies once. That is the immunity the minimum removes.
    assert 336_100 / (7 * 24) > 2_000
