"""Characterization tests for `cli.main`'s command dispatch.

Each subcommand's work is stubbed, so these pin only what `main` itself does:
which function a command reaches, what it prints, the exit code it returns,
and how the dataset and refusal errors map to exit codes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.orchestrator import cli, cost, external, report
from evals.orchestrator.dataset import DatasetError
from evals.orchestrator.runner import RunRefused


def test_validate_prints_the_dataset_composition(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load", lambda: ["case"])
    monkeypatch.setattr(cli, "_composition", lambda cases: f"composition of {cases}")
    assert cli.main(["validate"]) == cli.EXIT_OK
    assert capsys.readouterr().out == "composition of ['case']\n"


def test_estimate_prints_the_described_estimate(monkeypatch, capsys):
    seen: dict[str, Any] = {}

    def estimate(cases: list[Any], *, stages: str, reps: int) -> SimpleNamespace:
        seen.update(cases=cases, stages=stages, reps=reps)
        return SimpleNamespace(describe=lambda: "an estimate")

    monkeypatch.setattr(cli, "_cases", lambda args: ["c1"])
    monkeypatch.setattr(cost, "estimate", estimate)
    assert cli.main(["estimate", "--stages", "all", "--reps", "3"]) == cli.EXIT_OK
    assert capsys.readouterr().out == "an estimate\n"
    assert seen == {"cases": ["c1"], "stages": "all", "reps": 3}


def test_run_reaches_the_runner(monkeypatch, capsys):
    def cases(args: Any) -> list[Any]:
        raise DatasetError(f"{args.cmd} selected nothing")

    monkeypatch.setattr(cli, "_cases", cases)
    assert cli.main(["run"]) == cli.EXIT_USAGE
    assert capsys.readouterr().err == "dataset error: run selected nothing\n"


@pytest.mark.parametrize(("cmd", "printed"), [("report", "summary.md"), ("sweep", "sweep.md")])
def test_report_and_sweep_print_their_own_file(monkeypatch, capsys, tmp_path, cmd, printed):
    written: list[Path] = []

    def write(variant_dir: Path) -> tuple[str, str]:
        written.append(variant_dir)
        return "summary.md", "sweep.md"

    monkeypatch.setattr(report, "write", write)
    assert cli.main([cmd, str(tmp_path)]) == cli.EXIT_OK
    assert capsys.readouterr().out == f"{printed}\n"
    assert written == [tmp_path]


def test_compare_prints_and_optionally_writes(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(report, "compare", lambda before, after: f"{before.name} vs {after.name}")
    assert cli.main(["compare", str(tmp_path / "a"), str(tmp_path / "b")]) == cli.EXIT_OK
    assert capsys.readouterr().out == "a vs b\n"
    out = tmp_path / "cmp.md"
    assert cli.main(["compare", str(tmp_path / "a"), str(tmp_path / "b"), "--out", str(out)]) == cli.EXIT_OK
    assert out.read_text(encoding="utf-8") == "a vs b"
    assert capsys.readouterr().out == "a vs b\n"


def test_campaign_builds_and_prints_the_spend(monkeypatch, capsys, tmp_path):
    from evals.orchestrator import campaign

    calls: list[tuple[Path, Path]] = []

    def build(runs: Path, out: Path) -> dict[str, Any]:
        calls.append((runs, out))
        return {"grand_total_usd": 1.5}

    monkeypatch.setattr(campaign, "build", build)
    out = tmp_path / "out"
    assert cli.main(["campaign", "--runs", str(tmp_path), "--out", str(out)]) == cli.EXIT_OK
    assert calls == [(tmp_path, out)]
    assert capsys.readouterr().out == f"report: {out / 'REPORT.md'}; campaign spend {cost.usd(1.5)}\n"


def test_recheck_builds_and_prints_the_report_path(monkeypatch, capsys, tmp_path):
    from evals.orchestrator import recheck

    calls: list[tuple[Path, Path, Path]] = []
    monkeypatch.setattr(recheck, "build", lambda c, r, o: calls.append((c, r, o)))
    out = tmp_path / "out"
    code = cli.main(["recheck", "--campaign", str(tmp_path / "c"), "--runs", str(tmp_path / "r"), "--out", str(out)])
    assert code == cli.EXIT_OK
    assert calls == [(tmp_path / "c", tmp_path / "r", out)]
    assert capsys.readouterr().out == f"report: {out / 'REPORT.md'}\n"


def test_workers_reaches_the_worker_sample(capsys, tmp_path):
    assert cli.main(["workers", "--out", str(tmp_path)]) == cli.EXIT_REFUSED
    assert capsys.readouterr().err == "refused: the worker sample needs --live and --max-usd N\n"


def test_fetch_external_prints_one_line_per_benchmark(monkeypatch, capsys):
    keys = sorted(external.BENCHMARKS)[:2]

    def fetch(key: str) -> SimpleNamespace:
        return SimpleNamespace(
            benchmark=key, revision="0123456789abcdef", license="MIT", kept=3, skipped_length=1, skipped_duplicate=2
        )

    monkeypatch.setattr(external, "fetch", fetch)
    assert cli.main(["fetch-external", *keys]) == cli.EXIT_OK
    assert capsys.readouterr().out == "".join(
        f"{k}@0123456789ab (MIT): kept 3, skipped 1 out of length bounds, 2 duplicates\n" for k in keys
    )


def test_a_dataset_error_is_a_usage_error(monkeypatch, capsys):
    def load() -> list[Any]:
        raise DatasetError("bad row")

    monkeypatch.setattr(cli, "load", load)
    assert cli.main(["validate"]) == cli.EXIT_USAGE
    assert capsys.readouterr().err == "dataset error: bad row\n"


def test_a_refused_run_is_a_refusal(monkeypatch, capsys):
    def cases(args: Any) -> list[Any]:
        raise RunRefused("over the cap")

    monkeypatch.setattr(cli, "_cases", cases)
    assert cli.main(["run"]) == cli.EXIT_REFUSED
    assert capsys.readouterr().err == "refused: over the cap\n"


def test_a_command_main_does_not_know_is_a_usage_error(monkeypatch):
    parser = cli._parser()
    monkeypatch.setattr(cli, "_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda argv: SimpleNamespace(cmd="unknown"))
    assert cli.main([]) == cli.EXIT_USAGE
