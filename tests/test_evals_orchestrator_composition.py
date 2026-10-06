"""Composition measures for the planner eval: distinct specialists, recipe
coverage, irrelevant steps, handoff order — and the before/after comparison."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.orchestrator import cli, composition
from evals.orchestrator.app_pipeline import _default_planner
from evals.orchestrator.dataset import Case, PipelineLabel

NAMES = {"a1": "research.pro", "a2": "copywrite.v3", "a3": "code.gen", "a4": "code.next", "a5": "deploy.v0"}
WEBSITE = PipelineLabel("website", ("research.pro", "copywrite.v3", "code.gen"), ("deploy.v0",))


def _case(cid: str, label: PipelineLabel | None) -> Case:
    return Case(cid, "an intent", "allow", "moderate", "legit_moderate", "en", "-", pipeline=label)


def _row(cid: str, *agent_ids: str, category: str = "legit_moderate") -> dict[str, Any]:
    steps = [{"agent_id": a, "rationale": "r", "est_eta_seconds": 1.0, "tier": "low"} for a in agent_ids]
    return {
        "prompt_id": cid,
        "status": "ok",
        "tags": [category, "en", "orizon"],
        "grade": {"verdict_ok": 1},
        "meta": {"plan": {"steps": steps} if agent_ids else None},
    }


def test_checks_grade_a_full_ordered_unpadded_plan() -> None:
    assert composition.checks(["research.pro", "copywrite.v3", "code.gen", "deploy.v0"], WEBSITE) == {
        "plan_one_builder": 1,
        "plan_recipe_full": 1,
        "plan_no_irrelevant": 1,
        "plan_order_ok": 1,
    }


def test_checks_catch_a_gap_padding_disorder_and_two_builders() -> None:
    got = composition.checks(["code.gen", "research.pro", "ads.meta", "code.next"], WEBSITE)

    assert got == {"plan_one_builder": 0, "plan_recipe_full": 0, "plan_no_irrelevant": 0, "plan_order_ok": 0}
    # Unlabelled: only the builder rule applies.
    assert composition.checks(["code.gen"], None) == {"plan_one_builder": 1}


def test_measure_counts_specialists_coverage_and_irrelevant_steps() -> None:
    cases = {"c1": _case("c1", WEBSITE), "c2": _case("c2", WEBSITE), "c3": _case("c3", None)}
    rows = [
        _row("c1", "a1", "a2", "a3", "a5"),  # full, in order, nothing stray
        _row("c2", "a3", "a3", "a4"),  # one of three expected, a stray code.next, two builders
        _row("c3", "a3", category="legit_low"),  # unlabelled single step
        _row("c4"),  # no plan: not counted
    ]

    m = composition.measure(rows, cases, NAMES)

    assert m.plans == 3
    assert m.distinct == [4, 2, 1]
    assert m.distinct_mean == pytest.approx(7 / 3)
    assert (m.single_step.hits, m.single_step.n) == (1, 3)
    assert m.labelled == 2
    assert m.coverage == [1.0, pytest.approx(1 / 3)]
    assert (m.full_coverage.hits, m.full_coverage.n) == (1, 2)
    assert (m.irrelevant_steps.hits, m.irrelevant_steps.n) == (1, 7)
    assert m.irrelevant_agents == {"code.next": 1}
    assert (m.order_ok.hits, m.order_ok.n) == (2, 2)
    assert (m.one_builder.hits, m.one_builder.n) == (2, 3)
    assert "| website | 2 |" in "\n".join(composition.render(m))


def test_compare_reads_two_variants_on_their_shared_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def variant(name: str, rows: list[dict[str, Any]]) -> Path:
        d = tmp_path / name
        d.mkdir()
        (d / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return d

    before = variant("before", [_row("mod-001", "agt_11c0"), _row("low-001", "agt_01h8")])
    after = variant("after", [_row("mod-001", "agt_09l5", "agt_05x7", "agt_01h8", "agt_02k2", "agt_11c0", "agt_12r0")])
    out = tmp_path / "compare.md"

    assert cli.main(["compare", str(before), str(after), "--out", str(out)]) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "cases planned by both: 1" in text
    assert "| mean distinct specialists | 1.00 | 6.00 |" in text
    assert "| coverage: website | 16.7% | 100.0% |" in text
    assert text in capsys.readouterr().out


def test_the_eval_offers_the_planner_only_what_decompose_would(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents import registry
    from app.agents.workers.mock import MockWorker

    monkeypatch.setitem(registry.WORKERS, "agt_07w3", MockWorker("agt_07w3", "ads.meta"))

    _, block, offered = _default_planner()

    assert "agt_07w3" not in offered and "id=agt_07w3 " not in block
    assert "agt_11c0" in offered
