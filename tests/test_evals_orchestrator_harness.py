"""The eval runner end to end, on the oracle and null pipelines and scripted failures.

The audit's first rule for any eval: before it measures a model, show that a
pipeline which knows every answer scores ~100% and one that answers a constant
scores what a constant deserves. Then the plumbing: a failed attempt is never
a score, a re-run resumes without duplicating, and spend stops at the cap.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from evals.orchestrator import metrics, report
from evals.orchestrator.contract import (
    CaseRun,
    GuardObservation,
    PipelineUnavailable,
    PlanObservation,
    SpendCapReached,
    StageCall,
)
from evals.orchestrator.dataset import Case, load
from evals.orchestrator.runner import RunConfig, RunRefused, read_jsonl, run_cases
from evals.orchestrator.synthetic import NullPipeline, SyntheticPipeline


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load()


def _run(cases, pipeline, tmp_path: Path, **cfg):
    config = RunConfig(flow_dir=tmp_path, **cfg)
    outcome = asyncio.run(run_cases(cases, pipeline, config))
    return outcome, read_jsonl(config.variant_dir / "results.jsonl"), read_jsonl(config.variant_dir / "errors.jsonl")


def test_the_oracle_scores_full_marks_on_every_metric(cases, tmp_path):
    outcome, rows, errors = _run(cases, SyntheticPipeline(), tmp_path, stages="all")
    assert (outcome.scored, outcome.errors, errors) == (len(cases), 0, [])
    g = metrics.guard_metrics(rows)
    for key in ("verdict_accuracy", "injection_recall", "block_precision", "needs_detail_recall", "tier_accuracy"):
        assert g[key].value == 1.0, key
    assert g["false_block_rate"].hits == 0 and g["false_block_rate"].n == 86
    p = metrics.plan_metrics(rows)
    assert p["plan_valid"].value == 1.0 and p["plan_valid"].n == 86


def test_the_null_pipeline_catches_nothing_and_plans_nothing_valid(cases, tmp_path):
    _, rows, _ = _run(cases, NullPipeline(), tmp_path, stages="all")
    g = metrics.guard_metrics(rows)
    assert g["injection_recall"].hits == 0 and g["injection_recall"].n == 50
    assert g["needs_detail_recall"].hits == 0
    label, baseline = metrics.majority_baseline(rows)
    # A constant "allow" scores exactly the majority baseline — no better.
    assert label == "allow" and g["verdict_accuracy"].hits == baseline.hits
    assert metrics.plan_metrics(rows)["plan_valid"].hits == 0


def test_rows_carry_what_the_report_needs(cases, tmp_path):
    _, rows, _ = _run(cases[:3], SyntheticPipeline(), tmp_path, stages="all")
    for r in rows:
        assert r["prompt_id"] and r["prompt"] and r["tags"][0] == r["meta"]["expected"]["category"]
        assert r["split"] in ("train", "test") and r["status"] == "ok"
        assert set(r["usage"]) == {
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        }
        assert (tmp_path / "baseline" / "traces" / f"{r['prompt_id']}_rep0.json").exists()
    state = json.loads((tmp_path / "_state.json").read_text())
    assert state["metrics"][0]["id"] == "verdict_ok" and state["metrics"][0]["kind"] == "binary"
    assert set(state["train_ids"]) | set(state["test_ids"]) == {c.id for c in cases[:3]}


def test_a_rerun_resumes_without_duplicating(cases, tmp_path):
    subset = cases[:10]
    _run(subset, SyntheticPipeline(), tmp_path, reps=2)
    outcome, rows, _ = _run(subset, SyntheticPipeline(), tmp_path, reps=2)
    assert (outcome.scored, outcome.skipped_resume) == (0, 20)
    assert len({(r["prompt_id"], r["rep"]) for r in rows}) == len(rows) == 20


@dataclass
class Scripted:
    """Answers like the oracle, but fails the listed cases the listed way."""

    failures: dict[str, BaseException]
    name: str = "scripted"
    live: bool = False
    cost_per_case: float = 0.0
    calls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._oracle = SyntheticPipeline()

    def bind(self, cases):
        self._oracle.bind(cases)
        self._ids = {c.intent: c.id for c in cases}

    async def run(self, intent, *, stages):
        case_id = self._ids[intent]
        self.calls.append(case_id)
        err = self.failures.get(case_id)
        if isinstance(err, TimeoutError):
            await asyncio.sleep(5)
        elif err is not None:
            raise err
        run = await self._oracle.run(intent, stages=stages)
        run.calls.append(StageCall(stage="guard", model="jev", input_tokens=100, cost_usd=self.cost_per_case))
        return run


def test_failed_attempts_go_to_errors_never_to_results(cases, tmp_path):
    subset = cases[:6]
    billed = [StageCall(stage="guard", model="jev", input_tokens=500, cost_usd=0.01)]
    pipeline = Scripted(
        failures={
            subset[0].id: PipelineUnavailable("guard_unavailable", "jev and fallback down", billed),
            subset[1].id: TimeoutError(),
            subset[2].id: RuntimeError("bug in a stage"),
        }
    )
    outcome, rows, errors = _run(subset, pipeline, tmp_path, timeout_s=0.2)
    assert {r["prompt_id"] for r in rows} == {c.id for c in subset[3:]}
    by_case = {e["prompt_id"]: e for e in errors}
    assert by_case[subset[0].id]["failure_class"] == "guard_unavailable"
    assert by_case[subset[0].id]["cost_usd"] == 0.01  # billed-but-failed spend is still spend
    assert by_case[subset[1].id]["failure_class"] == "timeout"
    assert by_case[subset[2].id]["failure_class"] == "harness_error"
    assert outcome.spent_usd == pytest.approx(0.01)
    # A re-run retries exactly the failed cases.
    retry = Scripted(failures={})
    outcome2, rows2, _ = _run(subset, retry, tmp_path)
    assert sorted(retry.calls) == sorted(c.id for c in subset[:3])
    assert len(rows2) == 6


def test_the_spend_cap_stops_new_cases_before_it_is_passed(cases, tmp_path):
    subset = [c for c in cases if c.expected_verdict != "allow"][:20]  # guard-only cost per case
    pipeline = Scripted(failures={}, live=True, cost_per_case=0.001)
    # Each case's guard ceiling is tiny; a cap that fits a handful must stop the run there.
    outcome, rows, _ = _run(subset, pipeline, tmp_path, max_usd=0.0003, concurrency=1)
    assert outcome.stopped and outcome.skipped_budget > 0
    assert len(rows) < len(subset)


def test_a_live_pipeline_without_a_cap_is_refused(cases, tmp_path):
    with pytest.raises(RunRefused, match="spend cap"):
        _run(cases[:1], Scripted(failures={}, live=True), tmp_path)


def test_a_variant_cannot_mix_two_kinds_of_run(cases, tmp_path):
    _run(cases[:2], SyntheticPipeline(), tmp_path)
    with pytest.raises(RunRefused, match="another --variant"):
        _run(cases[:2], NullPipeline(), tmp_path)


def test_the_app_spend_cap_stops_the_run(cases, tmp_path):
    pipeline = Scripted(failures={cases[0].id: SpendCapReached("daily cap")})
    outcome, _, errors = _run(cases[:5], pipeline, tmp_path, concurrency=1)
    assert errors[0]["failure_class"] == "spend_cap"
    assert outcome.stopped and outcome.skipped_budget == 4


@dataclass
class Truncating:
    name: str = "truncating"
    live: bool = False

    async def run(self, intent, *, stages):
        return CaseRun(
            guard=GuardObservation("allow", "low", "low", (), {"injection": 0.0}),
            plan=PlanObservation(offered=frozenset({"agt_01h8"}), raw=None, truncated=True),
        )


def test_a_truncated_plan_is_counted_but_not_scored(cases, tmp_path):
    allow = [c for c in cases if c.expected_verdict == "allow"][:3]
    _, rows, _ = _run(allow, Truncating(), tmp_path, stages="all")
    assert {r["status"] for r in rows} == {"truncated"}
    assert metrics.guard_metrics(rows)["verdict_accuracy"].n == 0
    text = report.summarize(rows, [], live=False, pipeline="truncating")
    assert "truncated (not scored): 3" in text


def test_the_report_marks_a_fake_run_as_no_measurement(cases, tmp_path):
    _run(cases[:4], SyntheticPipeline(), tmp_path)
    summary, sweep_md = report.write(tmp_path / "baseline")
    assert "Not a model measurement" in summary.read_text()
    assert sweep_md.read_text().startswith("# Guard threshold sweep")
