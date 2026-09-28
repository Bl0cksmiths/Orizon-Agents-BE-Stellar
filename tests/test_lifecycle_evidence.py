"""The lifecycle harness's evidence file, run state and redaction (story 5.01)."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

from stellar_sdk import Keypair

from scripts.lifecycle.evidence import EvidenceLog, EvidenceRow, RunState, StateStore, render_markdown, tx_row
from scripts.lifecycle.redact import MASK, Console, Redactor

HASH = "ab" * 32


def _row(event: str = "plan", **kw: object) -> EvidenceRow:
    return EvidenceRow(stage="decompose", event=event, utc="2026-09-28T00:00:00Z", network="testnet", run_id="r1", **kw)  # type: ignore[arg-type]


def test_every_append_is_on_disk_and_rendered_before_it_returns(tmp_path: Path) -> None:
    log = EvidenceLog(tmp_path)
    log.append(_row(detail={"summary": "first"}))
    assert [json.loads(line)["seq"] for line in log.jsonl.read_text().splitlines()] == [1]
    assert "first" in log.markdown.read_text()
    log.append(
        tx_row(
            stage="verify",
            event="settle",
            network="testnet",
            run_id="r1",
            tx_hash=HASH,
            onchain_status="SUCCESS",
            contract="CESCROW",
            amount="0.0500000",
            asset="XLM (native)",
        )
    )
    rows = log.rows()
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[1]["explorer"] == f"https://stellar.expert/explorer/testnet/tx/{HASH}"
    md = log.markdown.read_text()
    assert f"[{HASH[:8]}…{HASH[-6:]}](https://stellar.expert/explorer/testnet/tx/{HASH})" in md
    assert "| SUCCESS | CESCROW |" in md and "0.0500000 XLM (native)" in md


def test_the_log_is_append_only_across_instances(tmp_path: Path) -> None:
    EvidenceLog(tmp_path).append(_row("a"))
    EvidenceLog(tmp_path).append(_row("b"))
    assert [r["event"] for r in EvidenceLog(tmp_path).rows()] == ["a", "b"]


def test_a_torn_last_line_does_not_lose_the_rows_before_it(tmp_path: Path) -> None:
    log = EvidenceLog(tmp_path)
    log.append(_row("a"))
    with log.jsonl.open("a") as fh:
        fh.write('{"stage": "verify", "event": "sett')  # the process died mid-write
    assert [r["event"] for r in log.rows()] == ["a"]
    log.append(_row("b"))
    assert [r["event"] for r in log.rows()] == ["a", "b"]


def test_markdown_lists_reputation_snapshots_in_their_own_table() -> None:
    rows = [
        {
            "seq": 1,
            "stage": "reputation",
            "event": "reputation_snapshot",
            "utc": "t",
            "agent": "ext",
            "network": "testnet",
            "detail": {"label": "start", "smoothed_bps": 7000, "lower_bound_bps": 5677, "source": "prior", "count": 0},
        }
    ]
    md = render_markdown(rows)
    assert "| 1 | start | t | ext | 7000 | 5677 | prior | 0 |" in md


def test_markdown_escapes_pipes_in_free_text() -> None:
    md = render_markdown([{"seq": 1, "stage": "s", "event": "e", "utc": "t", "detail": {"summary": "a | b"}}])
    assert "a \\| b" in md


def test_state_is_written_owner_only_and_round_trips(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = RunState(run_id="r1", agent="ext", task_id="tsk_1", authorize={"tx_hash": HASH, "status": "signed"})
    state.done("decompose")
    state.done("decompose")
    store.save(state)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert "state.json" in (tmp_path / ".gitignore").read_text().splitlines()
    loaded = store.load()
    assert loaded == state and loaded.completed == ["decompose"]


def test_state_ignores_fields_a_newer_harness_wrote(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({"run_id": "r1", "from_the_future": 1}))
    loaded = StateStore(tmp_path).load()
    assert loaded is not None and loaded.run_id == "r1"


# ── redaction ───────────────────────────────────────────────────
def test_registered_secrets_are_masked_everywhere() -> None:
    r = Redactor()
    r.register("operator-key-0123456789")
    r.register("rt_" + "a" * 32)
    line = "key operator-key-0123456789 token rt_" + "a" * 32 + " in https://x.test/?k=operator-key-0123456789"
    assert r.scrub(line) == f"key {MASK} token {MASK} in https://x.test/?k={MASK}"


def test_secret_shapes_are_masked_even_unregistered() -> None:
    kp = Keypair.random()
    xdr_like = "AAAAAgAAAAB+" + "Q" * 200 + "=="
    out = Redactor().scrub(f"seed {kp.secret} envelope {xdr_like} sig {'Zm9v' * 20}+w==")
    assert kp.secret not in out and xdr_like not in out and "Zm9v" not in out


def test_evidence_values_are_never_masked() -> None:
    kp = Keypair.random()
    link = f"https://stellar.expert/explorer/testnet/tx/{HASH}"
    line = f"buyer {kp.public_key} tx {HASH} {link} contract CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI"
    assert Redactor().scrub(line) == line


def test_short_values_are_not_treated_as_secrets() -> None:
    r = Redactor()
    r.register("abc")
    r.register(None)
    assert r.scrub("abc") == "abc"


def test_the_console_is_the_redacting_path() -> None:
    stream = io.StringIO()
    redactor = Redactor()
    redactor.register("super-secret-value")
    Console(redactor, stream).say("the value is super-secret-value")
    assert stream.getvalue() == f"the value is {MASK}\n"
