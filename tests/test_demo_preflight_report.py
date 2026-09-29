"""The pre-flight's verdict and its report shapes (story 5.04).

The verdict rule is the whole point of the tool — a take recorded on a
precondition nobody judged is the wasted session it exists to prevent — so it
is tested on its own, away from any deployment.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.demo_preflight.checks import FAIL, PASS, SKIPPED, WARN, Check, Facts
from scripts.demo_preflight.config import EXIT_GO, EXIT_INCOMPLETE, EXIT_NO_GO
from scripts.demo_preflight.redact import scrub
from scripts.demo_preflight.report import RunFacts, render_json, render_markdown, verdict, write

RUN = RunFacts(
    api="https://api.test",
    backend="https://be.test",
    frontend="https://fe.test",
    rpc_url="https://rpc.test",
    horizon_url="https://horizon.test",
    generated_at=1_790_000_000,
    generated_utc="2026-09-21T00:00:00Z",
)


def _check(status: str, *, required: bool = True, cid: str = "x.y") -> Check:
    return Check(cid, "x", "a check", required=required, status=status, detail="d", fix="" if status == PASS else "f")


def test_all_pass_is_go() -> None:
    assert verdict([_check(PASS), _check(PASS)])[0] == EXIT_GO


def test_warn_holds() -> None:
    assert verdict([_check(PASS), _check(WARN)])[0] == EXIT_GO


def test_a_required_skip_is_never_a_pass() -> None:
    code, line = verdict([_check(PASS), _check(SKIPPED)])
    assert code == EXIT_INCOMPLETE
    assert line.startswith("NO-GO") and "skipped is not a pass" in line


def test_an_advisory_skip_or_fail_does_not_gate() -> None:
    assert verdict([_check(PASS), _check(SKIPPED, required=False), _check(FAIL, required=False)])[0] == EXIT_GO


def test_a_required_fail_outranks_a_skip() -> None:
    code, line = verdict([_check(FAIL), _check(SKIPPED)])
    assert code == EXIT_NO_GO
    assert "1 required check(s) failed, 1 skipped" in line


def test_no_checks_at_all_is_not_a_fail() -> None:
    # Guard the arithmetic, not a real path: run_checks always returns checks.
    assert verdict([])[0] == EXIT_GO


def test_json_shape(tmp_path: Path) -> None:
    checks = [_check(PASS, cid="a.b"), _check(FAIL, cid="c.d")]
    facts = Facts(cold_start_seconds=12.5, disclosures=["The operator G… is a team wallet: say so on camera."])
    code, line = verdict(checks)
    body = render_json(checks, facts, RUN, code, line)
    assert body["verdict"] == "NO-GO" and body["exit_code"] == EXIT_NO_GO and body["network"] == "testnet"
    assert body["generated_at"] == 1_790_000_000 and body["cold_start_seconds"] == 12.5
    assert [c["id"] for c in body["checks"]] == ["a.b", "c.d"]
    assert set(body["checks"][0]) == {"id", "group", "title", "required", "status", "detail", "fix"}
    md_path, json_path = write(tmp_path, render_markdown(checks, facts, RUN, code, line), body)
    assert json.loads(json_path.read_text()) == body
    markdown = md_path.read_text()
    assert "| FAIL | `c.d` a check | yes | d | f |" in markdown
    assert "Backend cold start: 12.5 s" in markdown
    assert "## Disclose on camera" in markdown


def test_reports_mask_anything_shaped_like_a_secret(tmp_path: Path) -> None:
    seed = "S" + "B" * 55
    blob = "AAAAAgAAAAB" + "x/" * 30 + "=="
    check = Check("a.b", "a", "t", status=FAIL, detail=f"server said {seed} and {blob}", fix="f")
    code, line = verdict([check])
    body = render_json([check], Facts(), RUN, code, line)
    markdown = render_markdown([check], Facts(), RUN, code, line)
    for text in (json.dumps(body), markdown):
        assert seed not in text and blob not in text and "[redacted]" in text


def test_scrub_leaves_the_evidence_alone() -> None:
    tx = "ab" * 32
    account = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
    url = f"https://stellar.expert/explorer/testnet/tx/{tx}"
    text = f"{tx} {account} {url}"
    assert scrub(text) == text
