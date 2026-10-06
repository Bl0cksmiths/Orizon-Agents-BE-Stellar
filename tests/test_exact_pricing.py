"""Payment matches the decomposer's prices to the stroop (ADR 0015).

What the plan shows per step and in total is what the buyer authorizes, and
what is settled per delivered step plus what is returned is that authorization
exactly. These pin the plan half: every step carries its agent's price as an
integer `price_stroops`, the plan's total is their exact sum, the legacy float
fields are derived from those integers, and the plan names the asset the
stroops are in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace

import pytest

from app import money
from app.config import settings
from app.demo_kits import detect_kit
from app.schemas import DecomposeResponse, Plan, PlanStep, StoredPlan
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

TESTNET = "Test SDF Network ; September 2015"
TESTNET_NATIVE_SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"
FREE_FORM_INTENT = "write a launch email for a habit tracker"
KIT_INTENT = "tetris game in html"


def _info(agent_id: str) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=8000,
        lower_bound_bps=8000,
        avg_bps=8000,
        count=3,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(settings, "stellar_network_passphrase", TESTNET)
    monkeypatch.setattr(settings, "stellar_asset_sac", TESTNET_NATIVE_SAC)
    yield
    state.agents.clear()
    state.agents.update(saved)


def _planner(*agent_ids: str) -> Callable[[str], Awaitable[SimpleNamespace]]:
    async def _arun(_prompt: str) -> SimpleNamespace:
        steps = [
            # The model's own price is never trusted: the clamp re-prices from the registry.
            PlanStep(agent_id=a, rationale=f"step for {a}", est_price_usdc=9.99, est_eta_seconds=1.0)
            for a in agent_ids
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    return _arun


def _decompose(monkeypatch: pytest.MonkeyPatch, intent: str, *agent_ids: str) -> DecomposeResponse:
    async def _reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return {a.id: _info(a.id) for a in state.list_agents()}

    async def _no_think() -> None:
        return None

    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _planner(*agent_ids))
    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_think)
    return asyncio.run(orchestrator_svc.decompose(intent))


# ── the plan ────────────────────────────────────────────────────────────
def test_every_step_carries_its_agents_price_in_stroops(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _decompose(monkeypatch, FREE_FORM_INTENT, "agt_09l5", "agt_05x7", "agt_01h8")

    assert [(s.agent_id, s.price_stroops) for s in resp.steps] == [
        ("agt_09l5", 240_000),
        ("agt_05x7", 90_000),
        ("agt_01h8", 120_000),
    ]
    assert resp.total_stroops == 450_000
    # The legacy floats are derived from the integers, never summed as floats.
    assert [s.est_price_usdc for s in resp.steps] == [0.024, 0.009, 0.012]
    assert resp.total_usdc == money.stroops_to_float(resp.total_stroops) == 0.045


def test_the_stored_plan_total_is_the_total_the_buyer_was_shown(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """copywrite.v3 + seo.brief: 0.012 + 0.009 is 0.020999999999999998 in floats.

    The response said 0.021 (the stroops sum) while the stored plan — what
    `/execute` and every later read judge the run by — kept the float sum.
    """
    resp = _decompose(monkeypatch, FREE_FORM_INTENT, "agt_01h8", "agt_05x7")
    stored = state.plans[resp.plan_id]

    assert resp.total_usdc == 0.021
    assert stored.total_usdc == resp.total_usdc
    assert stored.plan.total_stroops == resp.total_stroops == 210_000
    assert [s.price_stroops for s in stored.plan.steps] == [s.price_stroops for s in resp.steps]


def test_the_kit_plan_is_priced_in_stroops_too(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert detect_kit(KIT_INTENT) is not None
    resp = _decompose(monkeypatch, KIT_INTENT)
    stored = state.plans[resp.plan_id]

    assert resp.steps
    for step in resp.steps:
        assert step.price_stroops == money.to_stroops(state.agents[step.agent_id].price)
    assert resp.total_stroops == sum(s.price_stroops for s in resp.steps)
    assert stored.plan.total_stroops == resp.total_stroops
    assert stored.total_usdc == resp.total_usdc == money.stroops_to_float(resp.total_stroops)


def test_the_plan_names_the_asset_its_stroops_are_in(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Testnet's escrow takes custody in the native SAC: the amounts are XLM, not USDC."""
    resp = _decompose(monkeypatch, FREE_FORM_INTENT, "agt_01h8")

    assert resp.asset == money.AssetInfo(code="XLM", issuer=None, decimals=7)
    assert state.plans[resp.plan_id].plan.asset == resp.asset
    assert resp.model_dump(mode="json")["asset"] == {"code": "XLM", "issuer": None, "decimals": 7}


def test_an_external_agents_seven_decimal_price_is_kept_whole(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's on-chain price arrives in stroops; nothing may round it to three places."""
    state.add_agent(state.agents["agt_01h8"].model_copy(update={"price": 0.0123457}))

    resp = _decompose(monkeypatch, FREE_FORM_INTENT, "agt_01h8")

    assert resp.steps[0].price_stroops == 123_457
    assert resp.total_stroops == 123_457


# ── the schema ──────────────────────────────────────────────────────────
def test_a_step_priced_in_stroops_derives_its_legacy_float() -> None:
    step = PlanStep(agent_id="a", rationale="r", price_stroops=125_000, est_eta_seconds=1.0)
    assert step.est_price_usdc == 0.0125


def test_the_stroops_win_over_a_float_that_disagrees() -> None:
    step = PlanStep(agent_id="a", rationale="r", price_stroops=125_000, est_price_usdc=9.0, est_eta_seconds=1.0)
    assert (step.price_stroops, step.est_price_usdc) == (125_000, 0.0125)


def test_a_step_stored_before_stroops_existed_still_loads_exactly() -> None:
    """Plans persisted before ADR 0015 carry only the float; it converts exactly."""
    old = {"agent_id": "a", "rationale": "r", "est_price_usdc": 0.054, "est_eta_seconds": 1.0}
    step = PlanStep.model_validate(old)
    assert (step.price_stroops, step.est_price_usdc) == (540_000, 0.054)


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -0.01])
def test_a_price_the_ledger_cannot_hold_is_refused_at_the_step(bad: float) -> None:
    with pytest.raises(ValueError):
        PlanStep(agent_id="a", rationale="r", est_price_usdc=bad, est_eta_seconds=1.0)


def test_a_step_without_any_price_is_refused() -> None:
    with pytest.raises(ValueError):
        PlanStep(agent_id="a", rationale="r", est_eta_seconds=1.0)


def test_a_plan_total_is_the_exact_sum_of_its_steps() -> None:
    plan = Plan(
        steps=[
            PlanStep(agent_id="a", rationale="r", est_price_usdc=0.1, est_eta_seconds=1.0),
            PlanStep(agent_id="b", rationale="r", est_price_usdc=0.2, est_eta_seconds=1.0),
        ]
    )
    assert plan.total_stroops == 3_000_000
    stored = StoredPlan(id="pln_x", intent="i", plan=plan, total_usdc=0.30000000000000004, total_eta=2.0)
    assert stored.total_usdc == 0.3  # derived, whatever a caller passed


def test_a_stored_plan_round_trips_with_its_prices() -> None:
    plan = Plan(steps=[PlanStep(agent_id="a", rationale="r", price_stroops=123_457, est_eta_seconds=1.0)])
    stored = StoredPlan(id="pln_rt", intent="i", plan=plan, total_usdc=0.0, total_eta=1.0)

    back = StoredPlan.model_validate_json(stored.model_dump_json())

    assert back == stored
    assert back.plan.total_stroops == 123_457
    assert back.total_usdc == 0.0123457


def test_a_response_total_is_derived_from_its_steps() -> None:
    resp = DecomposeResponse(
        plan_id="pln_r",
        intent="i",
        steps=[
            PlanStep(agent_id="a", rationale="r", est_price_usdc=0.012, est_eta_seconds=1.0),
            PlanStep(agent_id="b", rationale="r", est_price_usdc=0.009, est_eta_seconds=1.0),
        ],
        total_usdc=0.020999999999999998,
        total_eta=2.0,
    )
    assert (resp.total_stroops, resp.total_usdc) == (210_000, 0.021)


def test_the_total_is_priced_after_composition_drops_a_step(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The planner's composition rules drop a step with nothing to work on — here
    translate.42 on an English request naming no language — before the buyer
    sees the plan. Its price must leave the total with it: the buyer authorizes
    exactly the steps that will run."""
    resp = _decompose(monkeypatch, FREE_FORM_INTENT, "agt_09l5", "agt_10b6", "agt_01h8")
    stored = state.plans[resp.plan_id]

    assert "agt_10b6" not in [s.agent_id for s in resp.steps]
    assert any(n.agent_id == "agt_10b6" for n in resp.notices)
    assert resp.total_stroops == 240_000 + 120_000 == sum(s.price_stroops for s in resp.steps)
    assert stored.plan.total_stroops == resp.total_stroops
    assert stored.total_usdc == resp.total_usdc == 0.036
