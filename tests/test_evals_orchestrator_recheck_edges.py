"""Characterization tests for the release re-check report's edge rows.

Built over the recorded 2026-10-06 runs, with extra records for what those
runs never hit: a skipped run in each code section, a critic whose violations
were recorded at run time, a stream cut at its token ceiling and its time
budget, a worker with no ceiling, and worker records with no served model or
no output. Each assertion pins one rendered line exactly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from evals.orchestrator.recheck import build

REPORTS = Path(__file__).resolve().parents[1] / "evals" / "orchestrator" / "reports"
CALL = {
    "model": "claude-sonnet-5-5",
    "effort": None,
    "first_token_ms": 250,
    "output_tokens": 12000,
    "stop_reason": "max_tokens",
}


def _rec(**kw: Any) -> str:
    base: dict[str, Any] = {
        "worker": "code.gen",
        "label": None,
        "latency_s": 12.5,
        "cost_usd": 0.03,
        "calls": [],
        "error": None,
        "output": {},
    }
    base.update(kw)
    return json.dumps(base)


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    runs = tmp_path_factory.mktemp("runs") / "recheck"
    shutil.copytree(REPORTS / "2026-10-06-recheck", runs)
    workers, length, final = runs / "r2-workers", runs / "r3-code-length", runs / "r4-final"
    (workers / "zz_extra.json").write_text(_rec(worker="zz.extra", tier="complex", calls=[CALL]))
    (workers / "zz_empty.json").write_text(_rec(worker="zz.empty", output=None))
    skipped = json.dumps({"worker": "code.gen", "label": "code.gen#9", "skipped": "budget gone"})
    (length / "code_gen_9.json").write_text(skipped)
    (length / "code_critic_9.json").write_text(
        _rec(
            worker="code.critic",
            calls=[CALL],
            error="did not finish within 100 s",
            output={"summary": "Deferred: a | b", "critic_violations": ["v1", "v2"]},
        )
    )
    (length / "code_critic_9__index.html").write_text("<html>\n<p>a</p>\n</html>\n")
    (length / "code_gen_8.json").write_text(_rec(calls=[dict(CALL, response_model="m")], output={"summary": "later"}))
    (length / "code_gen_8__index.html").write_text("<html></html>\n")
    (final / "code_gen_9.json").write_text(skipped)
    (final / "zz_other_1.json").write_text(
        _rec(worker="zz.other", calls=[CALL], error="did not finish within 100 s", output={"summary": "no list"})
    )
    (final / "zz_other_1__index.html").write_text("<html>\n" + "<p>x</p>\n" * 300 + "</html>\n")
    out = tmp_path_factory.mktemp("out")
    build(REPORTS / "2026-10-06", runs, out)
    return (out / "REPORT.md").read_text(encoding="utf-8").splitlines()


def test_a_worker_without_a_served_model_or_output(report):
    assert "| zz.empty | default | not recorded (stream cut off) | 12.5 s | $0.0300 |  |" in report
    assert "| zz.extra | complex | claude-sonnet-5-5 | 12.5 s | $0.0300 |  |" in report


def test_a_skipped_run_is_a_not_run_row_in_both_code_sections(report):
    assert report.count("| code.gen#9 | not run — budget gone | | | | | | | | | | | |") == 2


def test_the_code_length_rows_for_recorded_critic_violations_and_cut_streams(report):
    assert (
        "| code.critic | not recorded | — | 0.25 s | 12.5 s | 12,000 | $0.0300 | 3 | 24 | "
        "on the draft it polished: v1; on the draft it polished: v2 | yes | yes | yes |"
    ) in report
    assert "| code.gen | m | — | 0.25 s | 12.5 s | 12,000 | $0.0300 | 1 | 14 | none | yes | no | yes |" in report
    assert "- **code.critic** (`r3-code-length/code_critic_9__index.html`): Deferred: a \\| b" in report
    assert "- **code.gen** (`r3-code-length/code_gen_8__index.html`): later" in report


def test_the_final_check_row_for_a_worker_without_a_ceiling(report):
    row = next(line for line in report if line.startswith("| zz.other |"))
    assert row.startswith("| zz.other | not recorded | — | 0.25 s | 12.5 s | 12,000 | $0.0300 | 302 | 2,715 | 9.0 | ")
    assert row.endswith(" | yes | yes |")
    assert "- **zz.other** (`r4-final/zz_other_1__index.html`): no list — no “Deferred: …” list" in report
    assert not any(line.startswith("- **code.gen#9**") for line in report)
