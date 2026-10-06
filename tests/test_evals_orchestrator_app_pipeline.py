"""The eval's app pipeline: the real guard, improver and planner code on FakeJev/FakeClaude.

The suite's `llm_offline` fixture keeps every transport off the network; the
pipeline swaps in label-scripted fakes behind its recording transports. These
tests show that the real guard reaches the same verdicts as the eval's replay
of its rule, that every billed call is booked to its case, and that a guard
outage lands in errors.jsonl rather than as a verdict.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.llm import claude, jev, testing
from evals.orchestrator import metrics
from evals.orchestrator.app_pipeline import AppPipeline, RecordingClaude, RecordingJev, seeded_agents
from evals.orchestrator.contract import PipelineUnavailable
from evals.orchestrator.dataset import load
from evals.orchestrator.runner import RunConfig, read_jsonl, run_cases
from evals.orchestrator.synthetic import SyntheticPipeline

NOISE, SEED = 0.15, 0


@pytest.fixture(scope="module")
def cases():
    return load()


def _run(cases, pipeline, flow, stages="guard"):
    asyncio.run(run_cases(cases, pipeline, RunConfig(flow_dir=flow, stages=stages)))
    return read_jsonl(flow / "baseline" / "results.jsonl"), read_jsonl(flow / "baseline" / "errors.jsonl")


def test_the_real_guard_agrees_with_the_eval_replay_case_for_case(cases, tmp_path):
    app_rows, errors = _run(cases, AppPipeline.create(live=False, cases=cases, noise=NOISE, seed=SEED), tmp_path / "a")
    replay_rows, _ = _run(cases, SyntheticPipeline(name="synthetic", noise=NOISE, seed=SEED), tmp_path / "b")
    assert errors == []
    app = {r["prompt_id"]: (r["meta"]["observed"]["verdict"], r["meta"]["observed"]["tier"]) for r in app_rows}
    replay = {r["prompt_id"]: (r["meta"]["observed"]["verdict"], r["meta"]["observed"]["tier"]) for r in replay_rows}
    assert app == replay
    # The noise makes some verdicts wrong, so this is a real comparison, not two 100%s.
    assert metrics.guard_metrics(app_rows)["verdict_accuracy"].value < 1.0


def test_every_guard_call_is_booked_to_its_case(cases, tmp_path):
    rows, _ = _run(cases[:5], AppPipeline.create(live=False, cases=cases), tmp_path)
    for r in rows:
        calls = r["meta"]["calls"]
        assert [c["stage"] for c in calls] == ["guard"]
        assert calls[0]["model"] == settings.typesafe_model
        assert r["usage"]["input_tokens"] == testing.DEFAULT_JEV_INPUT_TOKENS
        assert r["meta"]["observed"]["raw_tier"] in ("low", "moderate", "complex")
        assert set(r["meta"]["observed"]["scores"]) >= {"injection", "harmful", "severity", "real_request"}


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    rationale: str
    est_eta_seconds: float
    tier: str


class _Plan(BaseModel):
    """The planner contract's ModelPlan shape (agents.orchestrator.draft_plan)."""

    model_config = ConfigDict(extra="forbid")
    steps: list[_Step]


async def _draft_plan(request, *, tier, agents_block):
    """Stands in for the planner lane's draft_plan: one structured planner call."""
    text = request if isinstance(request, str) else request.summary
    return await claude.structured(
        purpose="planner",
        model="claude-opus-5-5",
        system=f"PLANNER\n\n{agents_block}",
        user=f"{text}\n\nThe request's overall complexity is {tier}. Return the plan.",
        schema=_Plan,
        max_tokens=4000,
        effort="medium",
    )


def _with_planner(pipeline: AppPipeline) -> AppPipeline:
    agents = seeded_agents()
    pipeline.planner = _draft_plan
    pipeline.agents_block = "AVAILABLE_AGENTS:\n" + "\n".join(f"- id={a.id} name={a.name}" for a in agents)
    pipeline.offered = frozenset(a.id for a in agents)
    return pipeline


def test_all_stages_run_improve_recheck_and_plan_for_allowed_cases(cases, tmp_path):
    legit = [c for c in cases if c.category == "legit_moderate"][:4]
    pipeline = _with_planner(AppPipeline.create(live=False, cases=cases, noise=0.0))
    rows, errors = _run(legit, pipeline, tmp_path, stages="all")
    assert errors == []
    for r in rows:
        stages = [c["stage"] for c in r["meta"]["calls"]]
        assert stages[0] == "guard" and "improve" in stages and stages.count("recheck") == 2 and stages[-1] == "plan"
        assert r["grade"]["plan_valid"] == 1
        assert r["meta"]["spec"]["goal"]
        assert "same_request" in r["meta"]["observed"]["scores"]
        assert r["model"] == "claude-opus-5-5"
        assert r["cost_usd"] > 0


def test_blocked_cases_never_reach_the_improver_or_planner(cases, tmp_path):
    injections = [c for c in cases if c.category == "injection_override"][:3]
    pipeline = _with_planner(AppPipeline.create(live=False, cases=cases, noise=0.0))
    rows, _ = _run(injections, pipeline, tmp_path, stages="all")
    for r in rows:
        assert r["meta"]["observed"]["verdict"] == "block"
        assert [c["stage"] for c in r["meta"]["calls"]] == ["guard"]
        assert r["meta"]["plan_ran"] is False


def test_a_guard_outage_is_an_error_not_a_verdict(cases):
    pipeline = AppPipeline.create(live=False, cases=cases)
    jev.set_transport(RecordingJev(testing.OfflineJev()))
    claude.set_transport(RecordingClaude(testing.OfflineClaude()))
    with pytest.raises(PipelineUnavailable) as caught:
        asyncio.run(pipeline.run(cases[0].intent, stages="guard"))
    assert caught.value.failure_class == "guard_unavailable"


def test_a_refused_plan_is_recorded_as_a_refusal(cases, tmp_path):
    legit = [c for c in cases if c.category == "legit_low" and c.language == "en"][:1]
    pipeline = _with_planner(AppPipeline.create(live=False, cases=cases, noise=0.0))
    inner = claude.get_transport().inner  # the scripted FakeClaude behind the recorder
    inner.refuse(purpose="planner", category="cyber")
    rows, _ = _run(legit, pipeline, tmp_path, stages="all")
    assert rows[0]["meta"]["plan_refused"] == "cyber"
    assert "plan_valid" not in rows[0]["grade"]
    assert rows[0]["meta"]["calls"][-1]["stop_reason"] == "refusal"  # billed even though refused


def test_all_stages_run_through_the_real_raw_planner(cases, tmp_path):
    from app.agents.orchestrator import ModelPlan

    legit = [c for c in cases if c.category == "legit_low"][:3] + [c for c in cases if c.category == "legit_complex"][
        :3
    ]
    pipeline = AppPipeline.create(live=False, cases=cases, stages="all", noise=0.0)
    rows, errors = _run(legit, pipeline, tmp_path, stages="all")
    assert errors == []
    fake = claude.get_transport().inner
    planner_calls = fake.calls_for("planner")
    assert len(planner_calls) == len(legit)
    # The planner saw the same AVAILABLE_AGENTS block decompose renders, and
    # effort followed each case's tier.
    assert all("AVAILABLE_AGENTS:" in c.system for c in planner_calls)
    assert {c.effort for c in planner_calls} == {"low", "high"}
    for r in rows:
        ModelPlan.model_validate(r["meta"]["plan"])
        assert r["grade"]["plan_valid"] == 1
        assert set(r["meta"]["offered"]) == {a.id for a in seeded_agents()}


def test_forced_fallback_answers_on_haiku_and_never_bills_jev(cases, tmp_path):
    from app.llm.tiers import guard_fallback_model

    pipeline = AppPipeline.create(live=False, cases=cases, noise=0.0, force_fallback=True)
    rows, errors = _run(cases[:8], pipeline, tmp_path)
    assert errors == []
    for r in rows:
        assert [(c["stage"], c["model"]) for c in r["meta"]["calls"]] == [("guard_fallback", guard_fallback_model())]
        assert "fallback" in r["meta"]["observed"]["reasons"]
        assert r["grade"]["verdict_ok"] == 1
        assert r["meta"]["observed"]["raw_tier"] in ("low", "moderate", "complex")


def test_a_dated_snapshot_is_priced_as_its_alias():
    from app.llm import spend
    from app.llm.claude import Attempt, Completion
    from evals.orchestrator.app_pipeline import list_price_usd

    usage = spend.Usage(input_tokens=1_000_000, output_tokens=100_000)
    snapshot = Completion(
        text="{}",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
        attempts=(Attempt("claude-haiku-4-5-20251001", usage),),
    )
    # $1/MTok in + $5/MTok out on Haiku 4.5, whatever id the API answered with.
    assert list_price_usd(snapshot) == pytest.approx(1.0 + 0.5)


def test_a_streamed_call_records_its_effort_and_first_token_time():
    from evals.orchestrator.app_pipeline import _CaseLog, _current

    fake = testing.FakeClaude().reply("<html>" + "x" * 300 + "</html>", purpose="worker.code.gen")
    claude.set_transport(RecordingClaude(fake))
    log = _CaseLog()
    token = _current.set(log)
    try:
        asyncio.run(
            claude.text(
                purpose="worker.code.gen",
                model="claude-sonnet-5-5",
                system="s",
                user="u",
                max_tokens=9_000,
                effort="low",
                stream=True,
            )
        )
    finally:
        _current.reset(token)
    call = log.calls[0]
    assert call.effort == "low" and call.first_token_ms is not None and call.first_token_ms <= call.latency_ms
