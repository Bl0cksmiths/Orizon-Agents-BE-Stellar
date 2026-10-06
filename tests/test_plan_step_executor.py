"""Every planned step says who runs it, and a built-in step on which model.

`executor` is "built_in" exactly when the agent has a local worker — the same
rule /execute uses for first-party — and "external" for an operator's bound
endpoint, whose model is never guessed. `model` is the step tier's Claude
model on the Claude provider and the agno worker model on OpenAI. Pinned on
every road a step takes into a plan: the Claude clamp, the legacy clamp, the
fallback plan and a curated kit, each with a mixed built-in/external plan
where one can be made.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.orchestrator import ModelPlan, PlannedStep
from app.config import settings
from app.llm.testing import FakeClaude, FakeJev, choice, score
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc
from app.services.prompt_improver import SpecDraft
from app.state import state

INTENT = "build a landing page for my bakery with opening hours"
KIT_INTENT = "make me a tetris game"
EXTERNAL = "ext_exec1"


class _Store:
    def __init__(self, *ids: str) -> None:
        self._ids = frozenset(ids)

    async def list_agent_ids(self) -> frozenset[str]:
        return self._ids


def _bind(monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _Store(*ids))
    asyncio.run(binding_registry.refresh_bound_ids())


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The seeded catalog plus one bound external agent, and no kit pause."""

    async def _no_pause() -> None:
        return None

    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_pause)
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    state.add_agent(
        Agent(
            id=EXTERNAL,
            name="indexed.exec",
            skills=["code", "html"],
            price=0.02,
            rep=4.0,
            status="online",
            runs=0,
            real=False,
        )
    )
    _bind(monkeypatch, EXTERNAL)
    yield
    _bind(monkeypatch)
    state.agents.clear()
    state.agents.update(saved)


def _screened(fake_claude: FakeClaude, fake_jev: FakeJev, plan: ModelPlan, *, tier: str = "complex") -> None:
    fake_jev.answer(
        {
            "injection": 0.02,
            "harmful": 0.01,
            "severity": score(0),
            "real_request": 0.96,
            "complexity": choice(tier),
        },
        purpose="guard.intent",
    )
    fake_claude.reply(
        SpecDraft(goal="g", deliverable="d", constraints=[], done_criteria=[], summary="s"), purpose="improve.spec"
    )
    fake_jev.answer({"injection": 0.02, "harmful": 0.01, "severity": score(0)}, purpose="guard.spec")
    fake_jev.answer({"same_request": 0.9}, purpose="guard.spec.same")
    fake_claude.reply(plan, purpose="planner")


def _stamps(resp: DecomposeResponse) -> list[tuple[str, str | None, str | None]]:
    stamped = [(s.agent_id, s.executor, s.model) for s in resp.steps]
    # What /execute will run carries the same stamps as what the card shows.
    assert [(s.agent_id, s.executor, s.model) for s in state.plans[resp.plan_id].plan.steps] == stamped
    return stamped


def _decompose(intent: str = INTENT) -> DecomposeResponse:
    return asyncio.run(orchestrator_svc.decompose(intent))


def test_a_mixed_claude_plan_names_each_built_in_steps_model_and_no_external_one(
    registry: None, fake_claude: FakeClaude, fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    plan = ModelPlan(
        steps=[
            PlannedStep(agent_id="agt_09l5", rationale="research", est_eta_seconds=1.0, tier="low"),
            PlannedStep(agent_id=EXTERNAL, rationale="build it", est_eta_seconds=1.0, tier="complex"),
            PlannedStep(agent_id="agt_11c0", rationale="polish it", est_eta_seconds=1.0, tier="moderate"),
        ]
    )
    _screened(fake_claude, fake_jev, plan)

    assert _stamps(_decompose()) == [
        ("agt_09l5", "built_in", "claude-haiku-4-5"),
        (EXTERNAL, "external", None),
        ("agt_11c0", "built_in", "claude-sonnet-5-5"),
    ]


def test_a_built_in_steps_model_follows_its_capped_tier(
    registry: None, fake_claude: FakeClaude, fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model is read off the tier AFTER the cap, so a step the planner
    # asked Opus for on a low request is named — and runs — on Haiku.
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    plan = ModelPlan(steps=[PlannedStep(agent_id="agt_11c0", rationale="build", est_eta_seconds=1.0, tier="complex")])
    _screened(fake_claude, fake_jev, plan, tier="low")

    assert _stamps(_decompose()) == [("agt_11c0", "built_in", "claude-haiku-4-5")]


def test_the_claude_fallback_step_is_stamped_too(
    registry: None, fake_claude: FakeClaude, fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    plan = ModelPlan(steps=[PlannedStep(agent_id="agt_99zz", rationale="x", est_eta_seconds=1.0, tier="low")])
    _screened(fake_claude, fake_jev, plan, tier="moderate")

    resp = _decompose()

    assert resp.planner_fallback is True
    assert _stamps(resp) == [("agt_01h8", "built_in", "claude-sonnet-5-5")]


def test_a_mixed_legacy_plan_names_the_openai_worker_model(registry: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")
    monkeypatch.setattr(settings, "worker_model", "gpt-test-worker")

    async def _arun(_prompt: str) -> Any:
        steps = [
            PlanStep(agent_id=a, rationale=a, est_price_usdc=0.0, est_eta_seconds=1.0) for a in ("agt_01h8", EXTERNAL)
        ]
        return SimpleNamespace(status=None, content=Plan(steps=steps))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _arun)

    assert _stamps(_decompose()) == [
        ("agt_01h8", "built_in", "gpt-test-worker"),
        (EXTERNAL, "external", None),
    ]


def test_kit_steps_are_built_in_on_their_role_tiers_model(
    registry: None, fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    fake_jev.answer(
        {
            "injection": 0.02,
            "harmful": 0.01,
            "severity": score(0),
            "real_request": 0.96,
            "complexity": choice("complex"),
        },
        purpose="guard.intent",
    )

    stamped = _stamps(_decompose(KIT_INTENT))

    assert {executor for _, executor, _ in stamped} == {"built_in"}
    assert dict((a, m) for a, _, m in stamped)["agt_11c0"] == "claude-sonnet-5-5"
    assert dict((a, m) for a, _, m in stamped)["agt_09l5"] == "claude-haiku-4-5"


def test_an_untiered_kit_step_names_the_model_its_worker_defaults_to(
    registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A kit planned while the request check was paused has no tiers; each
    # worker then runs on its own default tier, and that is the model named.
    from app.agents.registry import get_worker
    from app.llm.tiers import model_for

    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    step = PlanStep(agent_id="agt_11c0", rationale="r", est_price_usdc=0.0, est_eta_seconds=1.0)

    stamped = orchestrator_svc._with_executor(step)

    assert stamped.executor == "built_in"
    assert stamped.model == model_for(get_worker("agt_11c0").default_tier)  # type: ignore[union-attr]
