"""The eval's command line: the live gate, the estimate, and the benchmark fetcher.

No test here spends money or touches the network: the live gate is shown to
refuse before a pipeline exists, and the fetcher runs on httpx.MockTransport
with placeholder rows.
"""

from __future__ import annotations

import json

import httpx
import pytest

from evals.orchestrator import cli, cost, external
from evals.orchestrator.dataset import DatasetError, load


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for name in cli.KEY_ENV:
        monkeypatch.delenv(name, raising=False)


def test_live_without_a_cap_is_refused_before_anything_runs(tmp_path, capsys):
    code = cli.main(["run", "--live", "--flow-dir", str(tmp_path)])
    assert code == cli.EXIT_REFUSED
    out = capsys.readouterr()
    assert "estimate:" in out.out and "--max-usd" in out.err
    assert not (tmp_path / "baseline").exists()


def test_live_over_the_cap_is_refused_with_the_estimate(tmp_path, capsys):
    code = cli.main(["run", "--live", "--max-usd", "0.5", "--stages", "all", "--flow-dir", str(tmp_path)])
    assert code == cli.EXIT_REFUSED
    assert "over --max-usd" in capsys.readouterr().err


def test_live_without_keys_is_refused(tmp_path, capsys, monkeypatch):
    from app.config import settings

    for attr in ("anthropic_api_key", "typesafe_api_key"):
        if hasattr(settings, attr):
            monkeypatch.setattr(settings, attr, None)
    code = cli.main(["run", "--live", "--max-usd", "1", "--flow-dir", str(tmp_path)])
    assert code == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err and "TYPESAFE_API_KEY" in err


def test_live_only_applies_to_the_real_pipeline(tmp_path, capsys):
    code = cli.main(["run", "--live", "--max-usd", "1", "--pipeline", "oracle", "--flow-dir", str(tmp_path)])
    assert code == cli.EXIT_REFUSED


def test_a_fake_run_prints_the_estimate_first_and_writes_the_report(tmp_path, capsys):
    code = cli.main(["run", "--pipeline", "oracle", "--per-category", "2", "--flow-dir", str(tmp_path)])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.index("estimate:") < out.index("scored")
    assert "26 cases" in out
    assert (tmp_path / "baseline" / "summary.md").exists()


def test_the_estimate_counts_planning_only_for_expected_allows():
    cases = load()
    guard_only = cost.estimate(cases, stages="guard")
    full = cost.estimate(cases, stages="all", reps=2)
    assert guard_only.planned_cases == 0 and set(guard_only.by_stage_usd) == {"guard"}
    assert full.planned_cases == sum(c.expected_verdict == "allow" for c in cases)
    assert full.ceiling_usd > full.expected_usd > guard_only.expected_usd * 2
    assert cost.estimate(cases, stages="all", reps=1).expected_usd * 2 == pytest.approx(full.expected_usd)


def test_non_ascii_text_is_estimated_generously():
    assert cost.estimate_tokens("abc") == 1
    assert cost.estimate_tokens("日本語") == 3


def test_the_estimate_prices_match_the_app_table_when_it_exists():
    spend = pytest.importorskip("app.llm.spend")
    table = cost.prices()
    assert table["claude-opus-5-5"]["output"] == spend.price_for("claude-opus-5-5").output
    assert table["jev"]["input"] == spend.JEV_INPUT_USD_PER_MTOK


def _mock_hf(rows_by_offset, *, sha=external.BENCHMARKS["deepset"].revision, csvs=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/datasets/" in url:
            return httpx.Response(200, json={"sha": sha})
        if "datasets-server" in url:
            if request.url.params["split"] != "test":
                return httpx.Response(200, json={"rows": []})
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json={"rows": rows_by_offset.get(offset, [])})
        for name, body in (csvs or {}).items():
            if url.endswith(name):
                return httpx.Response(200, text=body)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _hf_row(text, label):
    return {"row": {"text": text, "label": label}}


def test_fetch_maps_labels_skips_out_of_bound_rows_and_repeats(tmp_path):
    page0 = [_hf_row(f"placeholder injection {i}", 1) for i in range(60)]
    page0 += [_hf_row(f"placeholder benign question {i}", 0) for i in range(38)]
    page0 += [_hf_row("x" * 600, 1), _hf_row("placeholder injection 0", 1)]
    page1 = [_hf_row("placeholder benign question last", 0)]
    report = external.fetch("deepset", client=_mock_hf({0: page0, 100: page1}), cache=tmp_path)
    assert (report.kept, report.skipped_length, report.skipped_duplicate) == (99, 1, 1)
    assert report.license == "apache-2.0"
    cases = external.load(["deepset"], cache=tmp_path)
    assert sum(c.expected_verdict == "block" for c in cases) == 60
    assert {c.category for c in cases} == {"ext_injection", "ext_benign"}
    manifest = json.loads(next(tmp_path.glob("*.manifest.json")).read_text())
    assert manifest["revision"] == external.BENCHMARKS["deepset"].revision


def test_fetch_refuses_a_revision_that_moved(tmp_path):
    with pytest.raises(external.FetchError, match="not the pinned"):
        external.fetch("deepset", client=_mock_hf({}, sha="0" * 40), cache=tmp_path)


def test_fetch_reads_the_pinned_csv_files(tmp_path):
    header = "Index,Goal,Target,Behavior,Category,Source\n"
    csvs = {
        "harmful-behaviors.csv": header + "1,placeholder harmful goal,t,b,Cat,src\n",
        "benign-behaviors.csv": header + "1,placeholder benign goal,t,b,Cat,src\n",
    }
    external.fetch("jbb", client=_mock_hf({}, csvs=csvs), cache=tmp_path)
    cases = {c.category: c for c in external.load(["jbb"], cache=tmp_path)}
    assert cases["ext_harmful"].expected_verdict == "block"
    assert cases["ext_benign"].expected_verdict == "not_block"


def test_loading_an_unfetched_benchmark_says_how_to_fetch_it(tmp_path):
    with pytest.raises(DatasetError, match="fetch-external jbb"):
        external.load(["jbb"], cache=tmp_path)


def test_a_case_expected_blocked_still_reserves_the_planning_ceiling():
    # A guard miss would send it to the planner, so the run's cap must hold
    # room for that before it starts.
    injection = next(c for c in load() if c.category == "injection_hidden")
    _, expected_ceiling = cost.case_estimate(injection, stages="all")
    assert cost.reservation(injection, stages="all") > expected_ceiling * 100
    assert cost.reservation(injection, stages="guard") == expected_ceiling


def test_the_fallback_guard_is_priced_as_haiku():
    cases = load()
    jev_only = cost.estimate(cases, stages="guard")
    haiku = cost.estimate(cases, stages="guard", fallback=True)
    assert haiku.expected_usd > jev_only.expected_usd * 20
    assert cost.reservation(cases[0], stages="guard", fallback=True) > cost.reservation(cases[0], stages="guard")


def test_the_worker_sample_is_refused_without_live_and_a_cap(tmp_path, capsys):
    assert cli.main(["workers", "--out", str(tmp_path)]) == cli.EXIT_REFUSED
    assert cli.main(["workers", "--live", "--max-usd", "0.1", "--out", str(tmp_path)]) == cli.EXIT_REFUSED
    assert "ceiling" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_the_sample_contract_fits_the_intent_bound():
    from evals.orchestrator.workers_sample import JOBS

    assert all(3 <= len(j.intent) <= 500 and len(j.rationale) <= 500 for j in JOBS)
    assert len({j.agent_id for j in JOBS}) == 7


def test_a_streamed_reply_is_cut_off_at_the_budget():
    from app.llm.claude import Attempt, ClaudeRequest, Completion
    from app.llm.spend import Usage
    from evals.orchestrator.workers_sample import BudgetedClaude, BudgetExceeded

    class Streaming:
        async def complete(self, request):
            for _ in range(1000):
                await request.on_text("x" * 350)  # ~100 tokens a delta
            return Completion(
                text="", stop_reason="end_turn", model=request.model, attempts=(Attempt(request.model, Usage()),)
            )

    request = ClaudeRequest(
        purpose="worker.code.gen",
        model="claude-sonnet-5-5",
        system="s",
        user="u",
        max_tokens=48_000,
        effort="medium",
        stream=True,
    )
    guard = BudgetedClaude(Streaming(), remaining=lambda: 0.01)
    with pytest.raises(BudgetExceeded) as caught:
        import asyncio

        asyncio.run(guard.complete(request))
    assert 0.0099 < caught.value.estimate_usd < 0.0115


def test_the_recheck_is_refused_when_its_measured_cost_does_not_fit(tmp_path, capsys):
    assert cli.main(["workers", "--recheck", "--live", "--max-usd", "0.05", "--out", str(tmp_path)]) == cli.EXIT_REFUSED
    assert "headroom" in capsys.readouterr().err
