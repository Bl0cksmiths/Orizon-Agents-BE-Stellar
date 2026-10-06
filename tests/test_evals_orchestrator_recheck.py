"""The release re-check report, built from oracle runs instead of paid ones."""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.orchestrator.dataset import load
from evals.orchestrator.recheck import build
from evals.orchestrator.runner import RunConfig, run_cases
from evals.orchestrator.synthetic import SyntheticPipeline


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    campaign, recheck = tmp_path_factory.mktemp("campaign"), tmp_path_factory.mktemp("recheck")
    cases = load()
    asyncio.run(run_cases(cases, SyntheticPipeline(), RunConfig(flow_dir=campaign, variant="s1-guard", reps=3)))
    asyncio.run(
        run_cases(
            cases, SyntheticPipeline(name="synthetic", noise=0.1), RunConfig(flow_dir=recheck, variant="r1-guard")
        )
    )
    workers = recheck / "r2-workers"
    workers.mkdir()
    for name, cost, error in (
        ("copywrite.v3", 0.002, None),
        ("code.gen", 0.0, "BudgetExceeded: stopped at an estimated $0.1296"),
    ):
        rec = {
            "worker": name,
            "tier": None,
            "latency_s": 1.0,
            "cost_usd": cost,
            "calls": [],
            "error": error,
            "output": {},
        }
        (workers / f"{name.replace('.', '_')}.json").write_text(json.dumps(rec))
    return campaign, recheck


def test_the_recheck_compares_both_runs_and_books_the_cut_off_estimate(runs, tmp_path):
    campaign, recheck = runs
    analysis = build(campaign, recheck, tmp_path)
    assert analysis["campaign_pooled"]["verdict_accuracy"].n == 504
    assert analysis["recheck"]["verdict_accuracy"].n == 168
    costs = json.loads((tmp_path / "costs.json").read_text())
    assert costs["cut_off_estimated_usd"] == {"code.gen": 0.1296}
    assert costs["total_with_estimates_usd"] == pytest.approx(costs["measured_total_usd"] + 0.1296)
    report = (tmp_path / "REPORT.md").read_text()
    assert "≈ $0.1296" in report and "| re-check, legit requests |" in report


def test_the_code_length_section_measures_the_saved_html(runs, tmp_path):
    campaign, recheck = runs
    d = recheck / "r3-code-length"
    d.mkdir(exist_ok=True)
    call = {
        "response_model": "claude-sonnet-5-5",
        "effort": "low",
        "first_token_ms": 1500,
        "output_tokens": 7000,
        "stop_reason": "end_turn",
    }
    rec = {
        "worker": "code.gen",
        "label": "code.gen#1",
        "latency_s": 39.0,
        "cost_usd": 0.07,
        "calls": [call],
        "error": None,
        "output": {"summary": "An app; deferred: reports"},
    }
    (d / "code_gen_1.json").write_text(json.dumps(rec))
    (d / "code_gen_1__index.html").write_text("<!doctype html>\n<html><body>tiny</body></html>\n")
    (d / "code_gen_2.json").write_text(
        json.dumps({"worker": "code.gen", "label": "code.gen#2", "skipped": "budget left 0.0213 USD"})
    )
    analysis = build(campaign, recheck, tmp_path)
    first, second = analysis["code_length"]
    assert first["lines"] == 2 and first["validator"] and first["deferred_listed"] is True
    assert first["hit_token_ceiling"] is False and first["hit_stream_budget"] is False
    assert second["skipped"].startswith("budget left")
    report = (tmp_path / "REPORT.md").read_text()
    assert "## Complex code.gen after the length fix" in report and "code.gen#2 | not run" in report
    assert (tmp_path / "r3-code-length" / "code_gen_1__index.html").exists()
