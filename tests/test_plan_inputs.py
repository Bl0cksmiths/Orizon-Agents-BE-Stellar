"""`PlanStep.inputs_from`: which earlier steps of the plan each step reads.

Derived in code from the workers' handoff map (`workers/context.CONSUMES`),
on the final step list, on both planning paths — never from the model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app.agents.orchestrator import ModelPlan, PlannedStep
from app.config import settings
from app.llm.testing import FakeClaude, FakeJev, choice, score
from app.schemas import Agent, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.prompt_improver import SpecDraft
from app.state import state

RESEARCH, SEO, COPY, DESIGN = "agt_09l5", "agt_05x7", "agt_01h8", "agt_02k2"
GEN, NEXT, CRITIC, DEPLOY = "agt_11c0", "agt_03d9", "agt_12r0", "agt_08j2"
OCR, TRANSLATE, ADS, AUDIT = "agt_06q4", "agt_10b6", "agt_07w3", "agt_04m1"


def _inputs(*agent_ids: str, kit: bool = False) -> list[list[int] | None]:
    steps = [PlanStep(agent_id=a, rationale="r", est_price_usdc=0.01, est_eta_seconds=1.0) for a in agent_ids]
    return [s.inputs_from for s in orchestrator_svc._with_inputs(steps, kit=kit)]


@pytest.fixture()
def seeded() -> Iterator[None]:
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


def test_the_website_pipeline_names_each_steps_real_sources(seeded: None) -> None:
    assert _inputs(RESEARCH, SEO, COPY, DESIGN, GEN, CRITIC, DEPLOY) == [
        [],  # research.pro reads only image text, audits and translations
        [1],  # seo.brief ← research
        [1, 2],  # copy ← brand, research
        [1, 2, 3],  # design ← brand, copy, research
        [1, 2, 3, 4],  # code.gen ← design, copy, brand, research
        [1, 2, 3, 4, 5],  # code.critic ← copy, design, the draft, brand, research
        [6],  # deploy.v0 seals the latest artifact: the reviewed build
    ]


def test_a_step_reads_only_what_the_handoff_map_gives_it(seeded: None) -> None:
    # copy and translate.42 read the audit's findings; ads.meta does not.
    assert _inputs(AUDIT, COPY, ADS, TRANSLATE) == [[], [1], [2], [1, 2, 3]]
    assert _inputs(DESIGN, AUDIT, ADS) == [[], [], [1]]


def test_deploy_seals_the_build_when_no_review_ran(seeded: None) -> None:
    assert _inputs(DESIGN, NEXT, DEPLOY) == [[], [1], [2]]


def test_a_role_run_twice_is_read_from_its_latest_run(seeded: None) -> None:
    assert _inputs(COPY, COPY, ADS) == [[], [], [2]]


def test_kit_builders_leave_out_the_briefs_the_kit_already_supplies(seeded: None) -> None:
    pipeline = (RESEARCH, SEO, DESIGN, GEN, CRITIC, DEPLOY)

    assert _inputs(*pipeline, kit=True)[3] == [3]  # tokens only
    assert _inputs(*pipeline)[3] == [1, 2, 3]


def test_an_operator_step_reads_everything_and_is_read_by_any_reader(seeded: None) -> None:
    state.add_agent(Agent(id="ext_op", name="op", skills=["x"], price=0.01, rep=4.0, status="online", runs=1))

    assert _inputs(COPY, "ext_op", ADS, OCR) == [[], [1], [1, 2], []]


def test_a_free_form_plan_carries_inputs_for_its_final_composed_order(
    seeded: None, fake_claude: FakeClaude, fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    fake_jev.answer(
        {
            "injection": 0.02,
            "harmful": 0.01,
            "severity": score(0),
            "real_request": 0.96,
            "complexity": choice("complex", confidence=0.9),
        },
        purpose="guard.intent",
    )
    fake_claude.reply(
        SpecDraft(goal="g", deliverable="d", constraints=[], done_criteria=["done"], summary="s"),
        purpose="improve.spec",
    )
    fake_jev.answer({"injection": 0.02, "harmful": 0.01, "severity": score(0)}, purpose="guard.spec")
    fake_jev.answer({"same_request": 0.92}, purpose="guard.spec.same")
    # Out of order on purpose: composition moves the review after the build.
    fake_claude.reply(
        ModelPlan(
            steps=[
                PlannedStep(agent_id=a, rationale=f"step {a}", est_eta_seconds=1.0, tier="moderate")
                for a in (CRITIC, COPY, GEN)
            ]
        ),
        purpose="planner",
    )

    resp = asyncio.run(orchestrator_svc.decompose("build a landing page for my bakery"))

    assert [s.agent_id for s in resp.steps] == [COPY, GEN, CRITIC]
    assert [s.inputs_from for s in resp.steps] == [[], [1], [1, 2]]
    stored = state.plans[resp.plan_id].plan.steps
    assert [s.inputs_from for s in stored] == [[], [1], [1, 2]]


def test_a_kit_plan_carries_inputs(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_pause() -> None:
        return None

    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", no_pause)

    resp = asyncio.run(orchestrator_svc.decompose("make me a tetris game"))

    assert [s.agent_id for s in resp.steps] == [RESEARCH, SEO, DESIGN, GEN, CRITIC, DEPLOY]
    assert [s.inputs_from for s in resp.steps] == [[], [1], [1, 2], [3], [1, 2, 3, 4], [5]]


def test_a_plan_stored_before_the_field_loads_with_none() -> None:
    step = PlanStep.model_validate({"agent_id": GEN, "rationale": "r", "est_price_usdc": 0.054, "est_eta_seconds": 1})

    assert step.inputs_from is None
