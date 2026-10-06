"""The live campaign's report builder, on oracle runs instead of paid ones.

It must compute every figure from the run files, keep benchmark prompt text
out of what it writes, and carry hand-written passages across a rebuild.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.orchestrator.campaign import build
from evals.orchestrator.dataset import Case, load
from evals.orchestrator.runner import RunConfig, run_cases
from evals.orchestrator.synthetic import SyntheticPipeline

SECRET_BENCHMARK_TEXT = "placeholder benchmark prompt that must never be written out"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("runs")
    cases = load()
    external = [
        Case("ext-x-0001", SECRET_BENCHMARK_TEXT, "block", None, "ext_injection", "und", "note", "bench@abc"),
        Case("ext-x-0002", SECRET_BENCHMARK_TEXT + " two", "not_block", None, "ext_benign", "und", "note", "bench@abc"),
    ]
    for variant, subset, stages, reps in (
        ("s1-guard", cases, "guard", 3),
        ("s2-external", external, "guard", 1),
        ("s3-haiku", cases, "guard", 1),
        ("s4-full", cases, "all", 1),
        ("smoke", cases[:2], "guard", 1),
    ):
        cfg = RunConfig(flow_dir=root, variant=variant, stages=stages, reps=reps)
        asyncio.run(run_cases(subset, SyntheticPipeline(name="synthetic", noise=0.15), cfg))
    workers = root / "s5-workers"
    workers.mkdir()
    (workers / "code_gen.json").write_text(
        json.dumps(
            {
                "worker": "code.gen",
                "agent_id": "agt_11c0",
                "case_id": "mod-008",
                "latency_s": 1.0,
                "cost_usd": 0.01,
                "calls": [{"model": "claude-sonnet-5-5"}],
                "error": None,
                "output": {"summary": "s", "counts": {"lines": 10}, "artifact_title": "T", "validator_violations": []},
            }
        )
    )
    (workers / "code_critic.json").write_text(
        json.dumps(
            {
                "worker": "code.critic",
                "agent_id": "agt_12r0",
                "case_id": "mod-008",
                "latency_s": 1.0,
                "cost_usd": 0.02,
                "calls": [],
                "error": None,
                "output": {"summary": "s"},
            }
        )
    )
    return root


def test_the_build_writes_every_deliverable(runs, tmp_path):
    analysis = build(runs, tmp_path)
    for name in ("REPORT.md", "metrics.json", "costs.json", "sweep.md"):
        assert (tmp_path / name).exists(), name
    planned = sum(1 for _ in (tmp_path / "plans").iterdir())
    assert planned == analysis["s4"]["pipeline"]["planned"] > 0
    assert json.loads((tmp_path / "costs.json").read_text())["grand_total_usd"] == pytest.approx(0.03)
    report = (tmp_path / "REPORT.md").read_text()
    assert "## 10. Error analysis" in report and "| tier accuracy" in report


def test_benchmark_prompt_text_never_leaves_the_runs(runs, tmp_path):
    build(runs, tmp_path)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET_BENCHMARK_TEXT not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_hand_written_passages_survive_a_rebuild(runs, tmp_path):
    build(runs, tmp_path)
    report = tmp_path / "REPORT.md"
    text = report.read_text().replace(
        "<!-- BEGIN narrative:summary -->\n_(to be written)_\n",
        "<!-- BEGIN narrative:summary -->\nThe owner's summary.\n",
    )
    report.write_text(text)
    build(runs, tmp_path)
    assert "The owner's summary." in report.read_text()
