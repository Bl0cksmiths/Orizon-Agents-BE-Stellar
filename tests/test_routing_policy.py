"""Routing policy: what a plan may use at all, before the floor or the model judge it.

Two rules, enforced in code (`orchestrator_svc.plannable`), on every planning
path — the Claude planner, the legacy planner and the curated kits:

  * a built-in agent whose worker only SIMULATES its output is never planned,
    since a buyer must never be charged for simulated work — and it is named
    on the plan card under `simulated_worker`;
  * an external operator agent is planned only while `PLANNER_ROUTE_EXTERNAL`
    is on (owner decision, 2026-10-06: plans use the platform's own agents).
    Off, it is named on the card under `external_not_routed` however good its
    reputation; on, it competes under the floor exactly as before.

The simulated agent here is installed by the test rather than taken from the
seeded catalog, so these tests keep meaning the same thing as the catalog's
simulated agents get real workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import registry as worker_registry
from app.agents.orchestrator import ModelPlan, PlannedStep
from app.agents.workers.mock import MockWorker
from app.config import settings
from app.llm.testing import FakeClaude, FakeJev, choice, score
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc, reputation_svc
from app.services.prompt_improver import SpecDraft
from app.services.reputation_svc import RepInfo
from app.state import state

EXTERNAL = "ext_star"  # bound, healthy, the best reputation in the registry
UNBOUND = "ext_idle"  # registered on-chain, nothing bound
SIMULATED = "agt_sim1"  # a built-in agent whose worker only simulates

INTENT = "build a landing page for my bakery with opening hours"
KIT_INTENT = "make me a tetris game"


class _Store:
    def __init__(self, *agent_ids: str) -> None:
        self._ids = frozenset(agent_ids)

    async def list_agent_ids(self) -> frozenset[str]:
        return self._ids


def _load_bound(monkeypatch: pytest.MonkeyPatch, *agent_ids: str) -> None:
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _Store(*agent_ids))
    asyncio.run(binding_registry.refresh_bound_ids())


def _rep(agent_id: str, bps: int) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=bps,
        lower_bound_bps=bps,
        avg_bps=bps,
        count=40,
        weight=40 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


def _onchain(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        name=f"operator.{agent_id}",
        skills=["code", "html", "ui", "tokens"],
        price=0.001,  # cheaper than every built-in agent, too
        rep=5.0,
        status="online",
        runs=900,
        source="onchain",
    )


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Seeded catalog + a top-rated bound external + an unbound one + a simulated
    built-in agent. Yields the agent-id lists each reputation read asked for."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    state.add_agent(_onchain(EXTERNAL))
    state.add_agent(_onchain(UNBOUND))
    state.add_agent(
        Agent(id=SIMULATED, name="sim.agent", skills=["copy"], price=0.005, rep=5.0, status="online", runs=1)
    )
    monkeypatch.setitem(worker_registry.WORKERS, SIMULATED, MockWorker(SIMULATED, "sim.agent"))
    _load_bound(monkeypatch, EXTERNAL)

    asked: list[list[str]] = []

    async def fetch_reps(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        asked.append(list(agent_ids))
        # The external agent outscores every built-in agent by a distance.
        return {a: _rep(a, 9900 if a == EXTERNAL else 8000) for a in agent_ids}

    monkeypatch.setattr(reputation_svc, "fetch_reps", fetch_reps)

    async def _no_pause() -> None:
        return None

    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_pause)
    yield asked
    _load_bound(monkeypatch)
    state.agents.clear()
    state.agents.update(saved)


@pytest.fixture()
def claude_on(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude, fake_jev: FakeJev) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")


def _screened(fake_claude: FakeClaude, fake_jev: FakeJev, planner: Any) -> None:
    """A clean guard, improver and re-check; `planner` answers the planning call."""
    fake_jev.answer(
        {
            "injection": 0.02,
            "harmful": 0.01,
            "severity": score(0),
            "real_request": 0.96,
            "complexity": choice("moderate", confidence=0.9),
        },
        purpose="guard.intent",
    )
    fake_claude.reply(
        SpecDraft(
            goal="Get a landing page for a bakery",
            deliverable="a single-page HTML landing page",
            constraints=["shows opening hours"],
            done_criteria=["the opening hours are visible"],
            summary="A one-page bakery website that shows its opening hours.",
        ),
        purpose="improve.spec",
    )
    fake_jev.answer({"injection": 0.02, "harmful": 0.01, "severity": score(0)}, purpose="guard.spec")
    fake_jev.answer({"same_request": 0.92}, purpose="guard.spec.same")
    if callable(planner):
        fake_claude.respond_with(planner, purpose="planner")
    else:
        fake_claude.reply(planner, purpose="planner")


def _plan(*agent_ids: str) -> ModelPlan:
    return ModelPlan(
        steps=[
            PlannedStep(agent_id=a, rationale=f"step for {a}", est_eta_seconds=1.0, tier="moderate") for a in agent_ids
        ]
    )


def _decompose(intent: str = INTENT) -> DecomposeResponse:
    return asyncio.run(orchestrator_svc.decompose(intent))


def _codes(resp: DecomposeResponse) -> dict[str, str]:
    return {n.agent_id: n.reason_code for n in resp.notices}


# ── PLANNER_ROUTE_EXTERNAL off (the default) ───────────────────


def test_the_switch_defaults_off() -> None:
    assert type(settings).model_fields["planner_route_external"].default is False


def test_a_top_rated_external_agent_is_never_offered_or_planned(
    registry: list[list[str]], claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    # The model names the external agent anyway — first, as the best-rated and
    # cheapest builder in the marketplace.
    _screened(fake_claude, fake_jev, _plan(EXTERNAL, "agt_11c0", "agt_12r0"))

    resp = _decompose()

    (call,) = fake_claude.calls_for("planner")
    assert f"id={EXTERNAL} " not in call.system and f"id={UNBOUND} " not in call.system
    assert [s.agent_id for s in resp.steps] == ["agt_11c0", "agt_12r0"]
    assert all(s.executor == "built_in" for s in resp.steps)
    # The card says why, in the policy's own words — not as an endpoint problem.
    assert _codes(resp)[EXTERNAL] == "external_not_routed"
    assert _codes(resp)[UNBOUND] == "external_not_routed"
    assert "unbound_endpoint" not in _codes(resp).values()
    # And its reputation was never even read: nothing could have used it.
    assert all(EXTERNAL not in ids for ids in registry)


def test_a_plan_of_only_external_steps_falls_back_to_a_built_in_agent(
    registry: list[list[str]], claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    _screened(fake_claude, fake_jev, _plan(EXTERNAL))

    resp = _decompose()

    assert resp.planner_fallback is True
    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]


def test_the_clamp_holds_the_policy_if_the_switch_turns_off_mid_plan(
    registry: list[list[str]], claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    # Offered under the switch ON; the switch is OFF by the time the plan
    # comes back. The clamp asks again at the point of use.
    settings.planner_route_external = True

    def planner(request: Any) -> ModelPlan:
        assert f"id={EXTERNAL} " in request.system
        settings.planner_route_external = False
        return _plan(EXTERNAL, "agt_11c0")

    _screened(fake_claude, fake_jev, planner)
    try:
        resp = _decompose()
    finally:
        settings.planner_route_external = False

    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]


def test_the_legacy_planner_holds_the_same_policy(registry: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")
    seen: list[str] = []

    async def arun(prompt: str) -> SimpleNamespace:
        seen.append(prompt)
        steps = [
            PlanStep(agent_id=a, rationale="model step", est_price_usdc=0.0, est_eta_seconds=1.0)
            for a in (EXTERNAL, SIMULATED, "agt_11c0")
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", arun)

    resp = _decompose("write a haiku about databases")

    assert f"id={EXTERNAL} " not in seen[0] and f"id={SIMULATED} " not in seen[0]
    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]
    assert _codes(resp)[EXTERNAL] == "external_not_routed"
    assert _codes(resp)[SIMULATED] == "simulated_worker"


def test_a_kit_never_promotes_an_external_agent_into_a_slot(
    registry: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # code.gen falls below the floor; the external agent shares its skills and
    # clears it, so with the switch ON it would stand in (test below).
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")

    async def fetch_reps(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        return {a: _rep(a, 100 if a == "agt_11c0" else 9900 if a == EXTERNAL else 8000) for a in agent_ids}

    monkeypatch.setattr(reputation_svc, "fetch_reps", fetch_reps)

    resp = _decompose(KIT_INTENT)

    assert EXTERNAL not in [s.agent_id for s in resp.steps]
    assert all(n.replacement_id != EXTERNAL for n in resp.notices)
    assert _codes(resp)[EXTERNAL] == "external_not_routed"

    monkeypatch.setattr(settings, "planner_route_external", True)
    resp = _decompose(KIT_INTENT)

    assert EXTERNAL in [s.agent_id for s in resp.steps]


def test_a_kit_holds_the_policy_if_the_switch_turns_off_during_its_pause(
    registry: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Snapshot taken with the switch ON, so the external agent is in the
    # substitute pool; the switch goes OFF during the kit's thinking pause,
    # and the point-of-use check keeps it out of code.gen's slot.
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")
    monkeypatch.setattr(settings, "planner_route_external", True)

    async def fetch_reps(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        return {a: _rep(a, 100 if a == "agt_11c0" else 9900 if a == EXTERNAL else 8000) for a in agent_ids}

    async def switch_off() -> None:
        settings.planner_route_external = False

    monkeypatch.setattr(reputation_svc, "fetch_reps", fetch_reps)
    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", switch_off)

    resp = _decompose(KIT_INTENT)

    assert EXTERNAL not in [s.agent_id for s in resp.steps]


# ── PLANNER_ROUTE_EXTERNAL on: today's behaviour ───────────────


def test_with_the_switch_on_an_external_agent_competes_on_merit(
    registry: list[list[str]],
    claude_on: None,
    fake_claude: FakeClaude,
    fake_jev: FakeJev,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "planner_route_external", True)
    _screened(fake_claude, fake_jev, _plan(EXTERNAL, "agt_12r0"))

    resp = _decompose()

    (call,) = fake_claude.calls_for("planner")
    assert f"id={EXTERNAL} " in call.system and f"id={UNBOUND} " not in call.system
    assert [s.agent_id for s in resp.steps] == [EXTERNAL, "agt_12r0"]
    assert resp.steps[0].executor == "external"
    codes = _codes(resp)
    assert EXTERNAL not in codes
    assert codes[UNBOUND] == "unbound_endpoint"
    assert "external_not_routed" not in codes.values()
    assert any(EXTERNAL in ids for ids in registry)


def test_with_the_switch_on_the_floor_still_judges_an_external_agent(
    registry: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "planner_route_external", True)
    reps = {a.id: _rep(a.id, 8000) for a in state.list_agents()}
    reps[EXTERNAL] = _rep(EXTERNAL, 0)

    shortlist = orchestrator_svc._routable_registry(reps)

    assert EXTERNAL not in shortlist.offered
    assert {n.agent_id: n.reason_code for n in shortlist.notices}[EXTERNAL] == "below_floor"


# ── simulated workers: never planned, whatever the switch ──────


@pytest.mark.parametrize("route_external", [False, True])
def test_a_simulated_agent_is_never_offered_or_planned(
    registry: list[list[str]],
    claude_on: None,
    fake_claude: FakeClaude,
    fake_jev: FakeJev,
    monkeypatch: pytest.MonkeyPatch,
    route_external: bool,
) -> None:
    monkeypatch.setattr(settings, "planner_route_external", route_external)
    _screened(fake_claude, fake_jev, _plan(SIMULATED, "agt_01h8"))

    resp = _decompose()

    (call,) = fake_claude.calls_for("planner")
    assert f"id={SIMULATED} " not in call.system
    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert _codes(resp)[SIMULATED] == "simulated_worker"
    assert all(SIMULATED not in ids for ids in registry)


def test_a_simulated_agent_is_never_a_floor_substitute(registry: list[list[str]]) -> None:
    # design.figma below the floor; the simulated agent shares its "figma"
    # skill and clears the floor, and nothing else off the kit pipeline that
    # may be planned shares a skill with it.
    designated = state.agents["agt_02k2"]
    state.agents[SIMULATED] = state.agents[SIMULATED].model_copy(update={"skills": ["figma"]})
    reps = {a.id: _rep(a.id, 8000) for a in state.list_agents()}
    reps[designated.id] = _rep(designated.id, 0)
    reps[SIMULATED] = _rep(SIMULATED, 9999)

    assert orchestrator_svc._floor_substitute(designated, reps, taken=set()) is None


def test_the_seeded_catalog_is_offered_exactly_where_a_real_worker_stands(registry: list[list[str]]) -> None:
    # True of whatever the catalog holds today: an agent becomes plannable the
    # moment its real worker replaces the simulation, with no planner change.
    seeded = [a for a in state.list_agents() if a.source == "seeded" and a.id != SIMULATED]

    for agent in seeded:
        worker = worker_registry.get_worker(agent.id)
        assert worker is not None
        assert orchestrator_svc.plannable(agent) is (worker.real is True), agent.id
