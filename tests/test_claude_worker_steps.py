"""Claude worker steps inside a run: the trace names the model, the tier reaches
the worker, and a step the model did not deliver is never charged.

The run loop's rule for a failed step (story 2.03) is that it is skipped, not
added to the run's spend — so neither the simulated total nor an on-chain
settle includes it — and the run carries on. These pin that a Claude refusal,
a reply cut off at max_tokens, an unreachable model and a model that never
answers each land on that rule, with the failure class in the trace and no
word the model wrote.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers import copywrite, sol_audit
from app.config import settings
from app.llm import claude as claude_layer
from app.llm.claude import ClaudeRequest, Completion
from app.llm.testing import FakeClaude
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.services import failure_tracker as ft
from app.state import state

INTENT = "a landing page for a neighbourhood bike repair co-op"
COPY_PRICE = 0.02
AUDIT_PRICE = 0.05
AUTH = "ab" * 16
PAYER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


class _TieredStep(PlanStep):
    """A plan step carrying a tier, as the planner lane's PlanStep will."""

    tier: str | None = None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """The execute-time re-check is held open (its own tests pin it), and the
    process-global failure streaks and test tasks are cleared either side."""
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)
    ft._streaks.clear()
    yield
    ft._streaks.clear()
    for tid in [t for t in state.tasks if t.startswith("tsk_cw_")]:
        state.tasks.pop(tid, None)
        state.traces.pop(tid, None)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _copy() -> copywrite.CopyOutput:
    return copywrite.CopyOutput(
        hero_headline="Fix it together",
        hero_subtitle="Free on Saturdays.",
        sections=[copywrite.Section(title="Tools", body="b1"), copywrite.Section(title="Classes", body="b2")],
    )


def _audit() -> sol_audit.AuditOutput:
    return sol_audit.AuditOutput(summary="No source; typical risks.", findings=[], cvss_estimate=2.0)


def _step(agent_id: str, price: float, tier: str | None = None) -> PlanStep:
    return _TieredStep(
        agent_id=agent_id,
        agent_name=WORKERS[agent_id].name,
        rationale="do it",
        est_price_usdc=price,
        est_eta_seconds=1.0,
        tier=tier,
    )


def _run(task_id: str, *steps: PlanStep, intent: str = INTENT, paid: bool = False) -> tuple[Task, list[str]]:
    plan = StoredPlan(
        id="pln_" + task_id,
        intent=intent,
        plan=Plan(steps=list(steps)),
        total_usdc=sum(s.est_price_usdc for s in steps),
        total_eta=1.0,
    )
    state.add_task(Task(id=task_id, intent=intent, agents=len(steps), spent=0.0, status="running"))
    if paid:
        asyncio.run(execution_svc._run(plan, task_id, auth_id_hex=AUTH, payer=PAYER))
    else:
        asyncio.run(execution_svc._run(plan, task_id))
    return state.tasks[task_id], [line.msg for line in state.traces[task_id]]


def test_each_step_names_its_model_and_runs_on_its_tier(claude: FakeClaude) -> None:
    claude.reply(_copy(), purpose="worker.copywrite.v3")
    claude.reply(_audit(), purpose="worker.sol-audit")
    task, trace = _run("tsk_cw_tiers", _step("agt_01h8", COPY_PRICE, "moderate"), _step("agt_04m1", AUDIT_PRICE))

    assert "copywrite.v3 on Claude Sonnet 5.5 (tier: moderate)" in trace
    assert "sol-audit on Claude Opus 5.5 (tier: complex)" in trace  # no tier: the worker's default
    assert not any("(fallback" in line for line in trace)
    assert [c.model for c in claude.calls] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert task.spent == pytest.approx(COPY_PRICE + AUDIT_PRICE)


@pytest.mark.parametrize(
    ("script", "rule"),
    [
        (lambda c: c.refuse(purpose="worker.sol-audit", category="cyber", explanation="MODEL-PROSE"), "model_refused"),
        (lambda c: c.truncate(purpose="worker.sol-audit", partial='{"summary": "'), "model_truncated"),
        (
            lambda c: c.reply({"summary": "s", "findings": [], "cvss_estimate": 42}, purpose="worker.sol-audit"),
            "invalid_output",
        ),
    ],
    ids=["refused", "truncated", "invalid"],
)
def test_a_step_the_model_did_not_deliver_is_failed_and_not_charged(claude: FakeClaude, script: Any, rule: str) -> None:
    claude.reply(_copy(), purpose="worker.copywrite.v3")
    script(claude)
    task, trace = _run("tsk_cw_" + rule, _step("agt_01h8", COPY_PRICE), _step("agt_04m1", AUDIT_PRICE))

    assert f"sol-audit failed ({rule})" in trace
    assert not any("MODEL-PROSE" in line for line in trace)
    # Only the delivered step is billed; the run still completes on it.
    assert task.spent == pytest.approx(COPY_PRICE)
    assert any(line.startswith("x402 payment → agt_01h8") for line in trace)
    assert not any(line.startswith("x402 payment → agt_04m1") for line in trace)
    assert ft.consecutive_failures("agt_04m1") == 1


class _NeverAnswers:
    """A Claude that accepts the request and never replies."""

    async def complete(self, request: ClaudeRequest) -> Completion:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


def test_a_model_that_never_answers_times_out_the_step_unbilled(
    claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 0.05)
    claude_layer.set_transport(_NeverAnswers())
    task, trace = _run("tsk_cw_timeout", _step("agt_04m1", AUDIT_PRICE))

    assert "sol-audit timed out" in trace
    assert task.spent == 0.0
    assert task.status == "failed"


def test_a_refused_polish_still_ships_code_gens_draft_unbilled(claude: FakeClaude) -> None:
    html = "<!doctype html><html><head><title>t</title></head><body><main>ok</main></body></html>"
    claude.reply(
        f"<artifact_title>Spoke</artifact_title><artifact_summary>s</artifact_summary><artifact_html>{html}</artifact_html>",
        purpose="worker.code.gen",
    )
    claude.refuse(purpose="worker.code.critic", category="cyber")
    task, trace = _run("tsk_cw_critic", _step("agt_11c0", 0.1), _step("agt_12r0", 0.04))

    assert "code.critic failed (model_refused)" in trace
    assert task.spent == pytest.approx(0.1)
    assert task.status == "complete"
    assert task.artifact is not None and task.artifact["title"] == "Spoke"


def test_a_kit_step_names_no_model(claude: FakeClaude) -> None:
    """A curated kit's deterministic step asks no model, so the trace names none."""
    task, trace = _run("tsk_cw_kit", _step("agt_02k2", 0.01), intent="build me a tetris game")

    assert claude.calls == []
    assert not any(line.startswith("design.figma on ") for line in trace)
    assert task.spent == pytest.approx(0.01)


# ── the fallback that served a step is the model the trace names ───────────


def test_a_step_a_fallback_served_names_the_fallback_model(claude: FakeClaude) -> None:
    claude.reply(_copy(), purpose="worker.copywrite.v3", served_by="claude-opus-5-5")
    claude.reply(_audit(), purpose="worker.sol-audit")
    _, trace = _run("tsk_cw_fallback", _step("agt_01h8", COPY_PRICE, "moderate"), _step("agt_04m1", AUDIT_PRICE))

    at = trace.index("copywrite.v3 on Claude Sonnet 5.5 (tier: moderate)")
    assert trace[at + 1] == "copywrite.v3 on Claude Opus 5.5 (fallback; tier: moderate)"
    # Collected per step: the next step, answered by its own model, names no fallback.
    assert [line for line in trace if "(fallback" in line] == [trace[at + 1]]
