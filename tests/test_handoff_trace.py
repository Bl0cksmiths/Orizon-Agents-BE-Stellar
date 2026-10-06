"""A run's pipeline hands each step's output forward, and the trace says so.

End to end through `execution_svc._run` on FakeClaude: a seven-step website
pipeline where each step's prompt carries what the steps before it produced,
with one "<agent> uses output from: …" line per step that builds on earlier
output — naming exactly the roles its prompt carried — and none for a step
that builds on nothing. A bound operator is sent `context` exactly as before
(nothing this lane adds rides along), and its line says what it receives.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers.base import Worker
from app.config import settings
from app.llm.testing import FakeClaude
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.services import failure_tracker as ft
from app.state import state

INTENT = "a landing page for a neighbourhood bike repair co-op"

TAGGED_APP = (
    "<artifact_title>Spoke</artifact_title><artifact_summary>A co-op site.</artifact_summary>"
    "<artifact_deferred>none</artifact_deferred><artifact_html><!doctype html><html><head>"
    '<meta charset="utf-8"><title>Spoke</title></head><body><main>APP</main></body></html></artifact_html>'
)
TOKENS = {
    **dict.fromkeys(["bg", "surface", "surface_2", "border", "text", "muted", "accent", "danger"], "#101010"),
    "primary": "#7C5CFF",
    "family_ui": "Inter, sans-serif",
    "family_display": "Georgia, serif",
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)
    ft._streaks.clear()
    yield
    ft._streaks.clear()
    for tid in [t for t in state.tasks if t.startswith("tsk_ho_")]:
        state.tasks.pop(tid, None)
        state.traces.pop(tid, None)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _step(agent_id: str, name: str | None = None) -> PlanStep:
    return PlanStep(
        agent_id=agent_id,
        agent_name=name or WORKERS[agent_id].name,
        rationale="do it",
        est_price_usdc=0.01,
        est_eta_seconds=1.0,
    )


def _run(task_id: str, *steps: PlanStep) -> tuple[Task, list[str]]:
    plan = StoredPlan(
        id="pln_" + task_id,
        intent=INTENT,
        plan=Plan(steps=list(steps)),
        total_usdc=sum(s.est_price_usdc for s in steps),
        total_eta=1.0,
    )
    state.add_task(Task(id=task_id, intent=INTENT, agents=len(steps), spent=0.0, status="running"))
    asyncio.run(execution_svc._run(plan, task_id))
    return state.tasks[task_id], [line.msg for line in state.traces[task_id]]


def _script_website(claude: FakeClaude) -> None:
    claude.reply(
        {
            "findings": [{"claim": f"RESEARCH-CLAIM-{i}", "confidence": 0.7} for i in range(3)],
            "sources": ["survey"],
            "summary": "RESEARCH-SUMMARY",
        },
        purpose="worker.research.pro",
    )
    claude.reply(
        {"keywords": ["SEO-KEYWORD"], "audiences": ["SEO-AUDIENCE"], "summary": "SEO-SUMMARY"},
        purpose="worker.seo.brief",
    )
    claude.reply(
        {
            "hero_headline": "COPY-HEADLINE",
            "hero_subtitle": "COPY-SUBTITLE",
            "sections": [{"title": "Tools", "body": "COPY-BODY-1"}, {"title": "Classes", "body": "COPY-BODY-2"}],
        },
        purpose="worker.copywrite.v3",
    )
    claude.reply(TOKENS, purpose="worker.design.figma")
    claude.reply(TAGGED_APP, purpose="worker.code.gen")
    claude.reply(TAGGED_APP.replace("A co-op site.", "Polished."), purpose="worker.code.critic")


WEBSITE = ["agt_09l5", "agt_05x7", "agt_01h8", "agt_02k2", "agt_11c0", "agt_12r0", "agt_08j2"]


def test_a_website_pipeline_hands_every_step_what_came_before(claude: FakeClaude) -> None:
    _script_website(claude)
    task, trace = _run("tsk_ho_site", *(_step(a) for a in WEBSITE))

    assert task.status == "complete"
    uses = [line for line in trace if " output from: " in line]
    assert uses == [
        "seo.brief uses output from: research.pro",
        "copywrite.v3 uses output from: seo.brief, research.pro",
        "design.figma uses output from: seo.brief, copywrite.v3, research.pro",
        "code.gen uses output from: design.figma, copywrite.v3, seo.brief, research.pro",
        "code.critic uses output from: code.gen, copywrite.v3, design.figma, seo.brief, research.pro",
        "deploy.v0 uses output from: code.critic",
    ]
    # Where each line sits: after the step is matched, before its model runs.
    i = trace.index("code.gen uses output from: design.figma, copywrite.v3, seo.brief, research.pro")
    assert trace[i - 1].startswith("match agent: code.gen")
    assert trace[i + 1].startswith("code.gen on Claude")

    def prompt(purpose: str) -> str:
        return claude.calls_for(purpose)[0].user

    assert "RESEARCH-CLAIM-0" in prompt("worker.seo.brief")
    assert "SEO-KEYWORD" in prompt("worker.copywrite.v3")
    assert "RESEARCH-SUMMARY" in prompt("worker.copywrite.v3")
    assert "COPY-HEADLINE" in prompt("worker.design.figma")
    assert "--primary: #7C5CFF;" in prompt("worker.code.gen")
    assert "COPY-BODY-2" in prompt("worker.code.gen")
    assert "--primary: #7C5CFF;" in prompt("worker.code.critic")
    assert "COPY-HEADLINE" in prompt("worker.code.critic")
    # The first step builds on nothing, so its prompt carries no handoff.
    assert "UPSTREAM_OUTPUTS" not in prompt("worker.research.pro")
    assert any(line.startswith("deploy.v0: sealed Spoke") for line in trace)


def test_a_step_with_nothing_upstream_gets_no_line(claude: FakeClaude) -> None:
    claude.reply(
        {"hero_headline": "H", "hero_subtitle": "S", "sections": [{"title": "a", "body": "b"}] * 2},
        purpose="worker.copywrite.v3",
    )
    _, trace = _run("tsk_ho_single", _step("agt_01h8"))
    assert not any(" output from: " in line for line in trace)


def test_a_failed_step_hands_nothing_forward(claude: FakeClaude) -> None:
    claude.refuse(purpose="worker.research.pro", category="cyber", explanation="no")
    claude.reply(
        {"hero_headline": "H", "hero_subtitle": "S", "sections": [{"title": "a", "body": "b"}] * 2},
        purpose="worker.copywrite.v3",
    )
    _, trace = _run("tsk_ho_failed", _step("agt_09l5"), _step("agt_01h8"))
    assert not any(" output from: " in line for line in trace)
    assert "UPSTREAM_OUTPUTS" not in claude.calls_for("worker.copywrite.v3")[0].user


class _Operator(Worker):
    """A bound operator: not in the registry, so the run loop treats it as third-party."""

    real = True

    def __init__(self) -> None:
        self.id = "agt_op_1"
        self.name = "external.agt_op_1"
        self.received: dict[str, Any] | None = None

    async def run(self, intent: str, rationale: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.received = dict(context or {})
        return {"summary": "operator delivered"}


def test_an_operator_receives_the_same_context_as_before_and_the_trace_says_so(
    claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude.reply(
        {"hero_headline": "H", "hero_subtitle": "S", "sections": [{"title": "a", "body": "b"}] * 2},
        purpose="worker.copywrite.v3",
    )
    operator = _Operator()

    async def _resolve(agent_id: str) -> Worker | None:
        return operator if agent_id == operator.id else WORKERS.get(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)
    _, trace = _run("tsk_ho_operator", _step("agt_01h8"), _step(operator.id, "operator"))

    assert "external.agt_op_1 receives output from: copywrite.v3" in trace
    assert operator.received is not None
    # Exactly the keys the envelope always carried: nothing this lane builds rides along.
    assert set(operator.received) == {"kit", "intent", "copywrite.v3"}
    assert operator.received["copywrite.v3"]["hero"]["headline"] == "H"


def test_a_trace_line_that_cannot_be_built_never_fails_the_step(
    claude: FakeClaude, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    claude.reply(
        {"hero_headline": "H", "hero_subtitle": "S", "sections": [{"title": "a", "body": "b"}] * 2},
        purpose="worker.copywrite.v3",
    )

    def _boom(context: Any) -> list[str]:
        raise RuntimeError("bug in the handoff")

    monkeypatch.setattr(WORKERS["agt_01h8"], "upstream_sources", _boom)
    task, trace = _run("tsk_ho_boom", _step("agt_01h8"))
    assert task.status == "complete"
    assert any(line.startswith("copywrite.v3: ") for line in trace)
    assert "could not name the upstream outputs of copywrite.v3" in caplog.text
