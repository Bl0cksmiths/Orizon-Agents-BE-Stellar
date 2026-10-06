"""Plan composition: a free-form plan is held to the handoffs its steps depend on.

Run end to end through `decompose` on the Claude pipeline (FakeJev /
FakeClaude), so a rule that is skipped, or applied before the clamp instead of
after it, fails here:

  * code.critic and deploy.v0 work on a code builder's output, so with no
    builder (code.gen / code.next) in the plan they are dropped — they would
    be paid steps on nothing;
  * vision.ocr reads an image the request supplies, so it is dropped when the
    request has none;
  * a step that reads another step's output runs after it: the critic after
    the builders, the seal after the builders and the critic;
  * nothing else moves, and nothing is ever added.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.agents.orchestrator import ModelPlan, PlannedStep
from app.config import settings
from app.llm.testing import FakeClaude, FakeJev, choice, score
from app.schemas import DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.prompt_improver import SpecDraft
from app.state import state

RESEARCH, SEO, COPY, DESIGN = "agt_09l5", "agt_05x7", "agt_01h8", "agt_02k2"
GEN, NEXT, CRITIC, DEPLOY = "agt_11c0", "agt_03d9", "agt_12r0", "agt_08j2"
OCR, TRANSLATE, ADS, AUDIT = "agt_06q4", "agt_10b6", "agt_07w3", "agt_04m1"

INTENT = "build a landing page for my bakery with opening hours"


@pytest.fixture()
def claude_on(fake_claude: FakeClaude, fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


def _plan_of(fake_claude: FakeClaude, fake_jev: FakeJev, *agent_ids: str) -> None:
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
        SpecDraft(
            goal="Get what the buyer asked for",
            deliverable="the deliverable",
            constraints=[],
            done_criteria=["it is delivered"],
            summary="What the buyer asked for.",
        ),
        purpose="improve.spec",
    )
    fake_jev.answer({"injection": 0.02, "harmful": 0.01, "severity": score(0)}, purpose="guard.spec")
    fake_jev.answer({"same_request": 0.92}, purpose="guard.spec.same")
    fake_claude.reply(
        ModelPlan(
            steps=[
                PlannedStep(agent_id=a, rationale=f"step {i} on {a}", est_eta_seconds=1.0, tier="moderate")
                for i, a in enumerate(agent_ids)
            ]
        ),
        purpose="planner",
    )


def _decompose(intent: str = INTENT) -> DecomposeResponse:
    return asyncio.run(orchestrator_svc.decompose(intent))


def _ids(resp: DecomposeResponse) -> list[str]:
    return [s.agent_id for s in resp.steps]


@pytest.mark.parametrize(
    "pipeline",
    [
        [RESEARCH, SEO, COPY, DESIGN, GEN, CRITIC],  # website
        [DESIGN, GEN, CRITIC, DEPLOY],  # web app, shipped
        [DESIGN, NEXT, CRITIC, DEPLOY],  # React / Next.js app
        [RESEARCH, SEO, COPY, ADS, TRANSLATE],  # marketing, localised
        [RESEARCH, COPY, TRANSLATE],  # report, translated
        [AUDIT, RESEARCH, COPY],  # smart contract
    ],
)
def test_a_pipeline_that_already_flows_forward_is_served_as_planned(
    claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev, pipeline: list[str]
) -> None:
    _plan_of(fake_claude, fake_jev, *pipeline)

    resp = _decompose()

    assert _ids(resp) == pipeline
    assert resp.planner_fallback is False


def test_a_review_with_no_build_before_it_is_dropped(
    claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    _plan_of(fake_claude, fake_jev, RESEARCH, COPY, CRITIC)

    resp = _decompose()

    assert _ids(resp) == [RESEARCH, COPY]
    # Nothing is billed for the dropped step: the plan total is its steps'.
    assert resp.total_stroops == sum(s.price_stroops for s in resp.steps)


def test_a_seal_with_no_build_before_it_is_dropped(claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev) -> None:
    _plan_of(fake_claude, fake_jev, COPY, DEPLOY)

    assert _ids(_decompose()) == [COPY]


def test_a_review_and_seal_planned_before_the_build_are_moved_after_it(
    claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    _plan_of(fake_claude, fake_jev, DEPLOY, CRITIC, DESIGN, GEN, COPY)

    # Only the two that read the build move; design and copy keep their order.
    assert _ids(_decompose()) == [DESIGN, GEN, CRITIC, DEPLOY, COPY]


def test_the_seal_follows_the_review_of_the_build(claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev) -> None:
    _plan_of(fake_claude, fake_jev, GEN, DEPLOY, CRITIC)

    assert _ids(_decompose()) == [GEN, CRITIC, DEPLOY]


def test_image_reading_without_an_image_is_dropped(claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev) -> None:
    _plan_of(fake_claude, fake_jev, OCR, TRANSLATE)

    resp = _decompose("translate the menu in my photo into Japanese")

    assert _ids(resp) == [TRANSLATE]
    # The buyer is told why the step they might expect is missing.
    assert [(n.agent_id, n.reason_code) for n in resp.notices if n.reason_code == "no_image_input"] == [
        (OCR, "no_image_input")
    ]


@pytest.mark.parametrize("link", ["http://example.com/menu.png", "https://127.0.0.1/menu.png"])
def test_a_link_the_image_fetch_would_refuse_is_no_image(
    claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev, link: str
) -> None:
    _plan_of(fake_claude, fake_jev, OCR, TRANSLATE)

    assert _ids(_decompose(f"translate the menu at {link} into Japanese")) == [TRANSLATE]


def test_a_text_plan_that_never_proposed_ocr_carries_no_image_notice(
    claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    _plan_of(fake_claude, fake_jev, COPY)

    assert all(n.reason_code != "no_image_input" for n in _decompose().notices)


def test_image_reading_with_an_image_link_is_kept(claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev) -> None:
    _plan_of(fake_claude, fake_jev, OCR, TRANSLATE)

    resp = _decompose("translate the menu at https://example.com/menu.png into Japanese")

    assert _ids(resp) == [OCR, TRANSLATE]
    assert all(n.reason_code != "no_image_input" for n in resp.notices)


def test_a_plan_left_with_nothing_to_do_takes_the_fallback(
    claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev
) -> None:
    _plan_of(fake_claude, fake_jev, CRITIC, DEPLOY)

    resp = _decompose()

    assert resp.planner_fallback is True
    assert _ids(resp) == [COPY]
    assert resp.stages[-1].msg == "None of the planner's steps could be used; a fallback plan was served"


def test_the_stored_plan_is_the_composed_one(claude_on: None, fake_claude: FakeClaude, fake_jev: FakeJev) -> None:
    _plan_of(fake_claude, fake_jev, CRITIC, GEN)

    resp = _decompose()

    assert _ids(resp) == [GEN, CRITIC]
    assert [s.agent_id for s in state.plans[resp.plan_id].plan.steps] == [GEN, CRITIC]


def test_the_legacy_planner_is_held_to_the_same_handoffs(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")

    async def arun(prompt: str) -> SimpleNamespace:
        steps = [
            PlanStep(agent_id=a, rationale=f"step on {a}", est_price_usdc=0.0, est_eta_seconds=1.0)
            for a in (CRITIC, COPY, GEN, OCR)
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", arun)
    try:
        resp = _decompose("write a launch page for my app")
    finally:
        state.agents.clear()
        state.agents.update(saved)

    assert _ids(resp) == [COPY, GEN, CRITIC]


@pytest.mark.parametrize(
    "proposed",
    [
        [CRITIC, CRITIC, DEPLOY, GEN, NEXT],
        [DEPLOY, OCR, COPY, CRITIC],
        [GEN, CRITIC, GEN, DEPLOY, CRITIC],
        [],
    ],
)
def test_composition_only_drops_or_reorders_never_adds(proposed: list[str]) -> None:
    steps = [
        PlanStep(agent_id=a, rationale=f"r{i}", est_price_usdc=0.01, est_eta_seconds=1.0)
        for i, a in enumerate(proposed)
    ]

    out = orchestrator_svc._compose(steps, "no image here").steps

    kept = [s.rationale for s in out]
    assert len(set(kept)) == len(kept)
    assert set(kept) <= {s.rationale for s in steps}
    # Every review and seal that survives sits after every builder.
    ids = [s.agent_id for s in out]
    builders = [i for i, a in enumerate(ids) if a in (GEN, NEXT)]
    for i, a in enumerate(ids):
        if a in (CRITIC, DEPLOY):
            assert builders and all(b < i for b in builders)
        if a == DEPLOY:
            assert all(c < i for c, x in enumerate(ids) if x == CRITIC)
