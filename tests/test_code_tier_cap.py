"""code.gen and code.critic run at most on the moderate tier (owner decision, option C).

Measured live on 2026-10-06: code.gen on Claude Sonnet 5.5 took 35–45 s for
$0.07–0.09, while Claude Opus 5.5 took 268 s for $0.68 — past the run loop's
120 s step deadline, which the v2 escrow's expiry math is built on. So a
complex code step is capped at moderate (Sonnet 5.5) rather than given more
time. The cap lives on the worker (`ModelWorker.max_tier`) and is read both
where the step runs and where the planner stamps the step's model, so the plan
card and the trace never claim Opus for a code step.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers.base import ModelWorker
from app.config import settings
from app.llm.testing import FakeClaude
from app.schemas import PlanStep
from app.services import orchestrator_svc

HAIKU, SONNET, OPUS = "claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5"
APP = "<!doctype html><html><head><title>t</title></head><body><main>ok</main></body></html>"
TAGGED = f"<artifact_title>T</artifact_title><artifact_summary>s</artifact_summary><artifact_html>{APP}</artifact_html>"


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _critic_context() -> dict[str, Any]:
    from app.agents.workers.code_gen import CodeGen, parse_tagged_artifact

    return {"code.gen": {"artifact": CodeGen()._artifact_dict(parse_tagged_artifact(TAGGED))}}


def test_only_the_two_code_workers_are_capped_and_at_moderate() -> None:
    caps = {w.name: w.max_tier for w in WORKERS.values() if isinstance(w, ModelWorker)}
    assert {name: cap for name, cap in caps.items() if cap is not None} == {
        "code.gen": "moderate",
        "code.critic": "moderate",
    }


@pytest.mark.parametrize(("agent_id", "context"), [("agt_11c0", None), ("agt_12r0", "critic")])
def test_a_complex_code_step_runs_on_sonnet_not_opus(agent_id: str, context: Any, claude: FakeClaude) -> None:
    claude.reply(TAGGED)
    ctx = _critic_context() if context else None
    asyncio.run(WORKERS[agent_id].run("build a dashboard", "r", context=ctx, tier="complex"))
    assert (claude.calls[0].model, claude.calls[0].effort) == (SONNET, "medium")


@pytest.mark.parametrize(("tier", "model"), [("low", HAIKU), ("moderate", SONNET), (None, SONNET)])
def test_the_cap_never_raises_a_code_steps_tier(tier: Any, model: str, claude: FakeClaude) -> None:
    claude.reply(TAGGED)
    asyncio.run(WORKERS["agt_11c0"].run("build a dashboard", "r", tier=tier))
    assert claude.calls[0].model == model


def test_an_uncapped_worker_still_reaches_opus(claude: FakeClaude) -> None:
    claude.reply({"summary": "s", "findings": [], "cvss_estimate": 1.0})
    asyncio.run(WORKERS["agt_04m1"].run("audit my vault", "r", tier="complex"))
    assert claude.calls[0].model == OPUS


def test_the_trace_names_the_capped_model(claude: FakeClaude) -> None:
    assert WORKERS["agt_11c0"].step_model("complex", None) == "Claude Sonnet 5.5 (tier: moderate)"
    assert WORKERS["agt_12r0"].step_model("complex", _critic_context()) == "Claude Sonnet 5.5 (tier: moderate)"


def _stamped(agent_id: str, tier: Any) -> str | None:
    step = PlanStep(
        agent_id=agent_id, agent_name=agent_id, rationale="r", est_price_usdc=0.01, est_eta_seconds=1.0, tier=tier
    )
    return orchestrator_svc._with_executor(step).model


@pytest.mark.parametrize(
    ("agent_id", "tier", "model"),
    [
        ("agt_11c0", "complex", SONNET),
        ("agt_12r0", "complex", SONNET),
        ("agt_11c0", "low", HAIKU),
        ("agt_11c0", None, SONNET),
        ("agt_04m1", "complex", OPUS),
        ("agt_01h8", None, HAIKU),
    ],
)
def test_the_planner_stamps_the_model_the_step_will_run_on(
    agent_id: str, tier: Any, model: str, claude: FakeClaude
) -> None:
    assert _stamped(agent_id, tier) == model
