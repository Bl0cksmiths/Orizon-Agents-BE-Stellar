"""The demo evidence sheet end to end, against an in-memory RPC and Horizon (story 5.04).

Every test drives `scripts.demo_evidence.cli.main` as an operator would, on
evidence files in the lifecycle harness's row shape. The rule under test: only
a hash the ledger re-verifies SUCCESS reaches `evidence.json` or the
description; every other one is listed in the sheet as failed; and a row from
another network stops the run before anything is read or written.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.demo_evidence.cli import main
from scripts.demo_evidence.config import (
    EXIT_NOT_SUCCESS,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    KINDS,
)
from scripts.demo_evidence.fakes import HORIZON, MAINNET_PASSPHRASE, RPC, FakeLedger, row, tx_hash, write_jsonl

EXPLORER = re.compile(r"^https://stellar\.expert/explorer/testnet/tx/[0-9a-f]{64}$")


@dataclass
class Outcome:
    code: int
    out: str
    dir: Path

    def json(self) -> dict[str, Any]:
        return json.loads((self.dir / "evidence.json").read_text())

    def sheet(self) -> str:
        return (self.dir / "evidence-sheet.md").read_text()

    def description(self) -> str:
        return (self.dir / "description.txt").read_text()


def run(ledger: FakeLedger, tmp_path: Path, *inputs: Path, extra: tuple[str, ...] = ()) -> Outcome:
    stream = io.StringIO()
    out_dir = tmp_path / "video"
    code = main(
        [*(str(i) for i in inputs), "--out-dir", str(out_dir), "--rpc-url", RPC, "--horizon-url", HORIZON, *extra],
        transport=ledger.transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_000.0,
    )
    return Outcome(code, stream.getvalue(), out_dir)


def full_run(ledger: FakeLedger, tmp_path: Path, name: str = "take-1") -> tuple[Path, dict[str, str]]:
    """One recording's evidence: every kind of transaction the harness writes, all on the ledger."""
    hashes = {k: ledger.add(f"{name}-{k}") for k in ("authorize", "settle", "seal", "rating", "refund", "dispute")}
    rows = [
        row("preflight", None, network="unknown", seq=1),
        row("authorize", hashes["authorize"], seq=2, amount="0.0200000"),
        row("settle", hashes["settle"], stage="verify", seq=3, amount="0.0100000"),
        row("seal", hashes["seal"], stage="verify", seq=4),
        row("rating", hashes["rating"], stage="poll", seq=5),
        row("refund", hashes["refund"], stage="refund", seq=6, amount="0.0050000"),
        row("dispute_rating", hashes["dispute"], stage="refund", seq=7),
    ]
    return write_jsonl(tmp_path / name / "lifecycle.jsonl", rows), hashes


# ── all verified ────────────────────────────────────────────────
def test_every_verified_hash_is_published_in_the_frozen_shape(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, hashes = full_run(ledger, tmp_path)
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_OK, out.out
    body = out.json()
    assert set(body) == {"generated_at", "network", "items"}
    assert body["generated_at"] == 1_790_000_000 and body["network"] == "testnet"
    assert [i["tx_hash"] for i in body["items"]] == [
        hashes[k] for k in ("authorize", "settle", "seal", "rating", "refund", "dispute")
    ]
    for item in body["items"]:
        assert set(item) == {"label", "deliverable", "kind", "tx_hash", "explorer", "verified"}
        assert isinstance(item["label"], str) and item["label"]
        assert item["deliverable"] in ("D1", "D2", "D3", "D4")
        assert item["kind"] in KINDS
        assert re.fullmatch(r"[0-9a-f]{64}", item["tx_hash"])
        assert item["explorer"] == f"https://stellar.expert/explorer/testnet/tx/{item['tx_hash']}"
        assert EXPLORER.match(item["explorer"])
        assert item["verified"] is True
    assert [(i["kind"], i["deliverable"]) for i in body["items"]] == [
        ("authorize", "D4"),
        ("settle", "D4"),
        ("seal", "D4"),
        ("rating", "D2"),
        ("refund", "D3"),
        ("dispute_rating", "D3"),
    ]


def test_the_description_lists_every_verified_hash_with_its_link(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, hashes = full_run(ledger, tmp_path)
    text = run(ledger, tmp_path, path, extra=("--title", "Orizon demo")).description()
    assert text.startswith("Orizon demo\n")
    assert "Chapters\n00:00 [CHAPTERS" in text
    for h in hashes.values():
        assert h in text and f"https://stellar.expert/explorer/testnet/tx/{h}" in text
    assert "Limitations. Everything in this video runs on the Stellar TESTNET" in text


def test_the_sheet_is_an_ordered_table_with_status_and_deliverable(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, hashes = full_run(ledger, tmp_path)
    sheet = run(ledger, tmp_path, path).sheet()
    assert "**6 of 6 transaction(s) re-verified SUCCESS**" in sheet
    lines = [line for line in sheet.splitlines() if line.startswith("| ") and "`" in line]
    assert len(lines) == 6
    assert lines[0].startswith("| 1 | authorize · Buyer authorization — alpha (0.0200000 XLM) |")
    assert "| D4 |" in lines[0] and f"`{hashes['authorize']}`" in lines[0] and "| SUCCESS | SUCCESS (rpc) |" in lines[0]
    assert "| D3 |" in lines[5] and "kind=dispute rating" in lines[5]
    assert "Failed verification" not in sheet


def test_several_runs_keep_their_order_and_a_repeated_hash_once(tmp_path: Path) -> None:
    ledger = FakeLedger()
    first, h1 = full_run(ledger, tmp_path, "take-1")
    second, h2 = full_run(ledger, tmp_path, "take-2")
    # A resumed run re-recorded take-1's refund.
    rows = [json.loads(line) for line in second.read_text().splitlines()]
    rows.append(row("refund", h1["refund"], seq=8))
    write_jsonl(second, rows)
    out = run(ledger, tmp_path, first.parent, second)  # a directory and a file
    assert out.code == EXIT_OK, out.out
    items = [i["tx_hash"] for i in out.json()["items"]]
    assert items[:6] == list(h1.values()) and items[6:] == list(h2.values())
    assert "1 repeated hash(es)" in out.sheet()


def test_a_hash_past_the_rpc_window_is_verified_on_horizon(tmp_path: Path) -> None:
    ledger = FakeLedger()
    old = ledger.add("old", rpc=False)
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("settle", old)])
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_OK
    assert out.json()["items"][0]["tx_hash"] == old
    assert "SUCCESS (horizon)" in out.sheet()


def test_an_rpc_outage_falls_back_to_horizon(tmp_path: Path) -> None:
    ledger = FakeLedger(rpc_down=True)
    h = ledger.add("x")
    out = run(ledger, tmp_path, write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("seal", h)]))
    assert out.code == EXIT_OK
    assert out.json()["items"][0]["tx_hash"] == h


def test_only_reads_are_made(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, _ = full_run(ledger, tmp_path)
    run(ledger, tmp_path, path)
    assert {c.split(" ")[1] for c in ledger.calls if c.startswith("rpc ")} == {"getNetwork", "getTransaction"}
    assert all(c.startswith("horizon GET") for c in ledger.calls if c.startswith("horizon"))


# ── unverified hashes ───────────────────────────────────────────
def test_a_failed_transaction_is_never_published(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, hashes = full_run(ledger, tmp_path)
    ledger.txs[hashes["refund"]].status = "FAILED"
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_NOT_SUCCESS
    assert hashes["refund"] not in json.dumps(out.json())
    assert hashes["refund"] not in out.description()
    sheet = out.sheet()
    assert "## Failed verification — NOT in evidence.json or the description" in sheet
    assert f"`{hashes['refund']}`" in sheet.split("## Failed verification")[1]
    assert "**5 of 6 transaction(s) re-verified SUCCESS**" in sheet
    assert len(out.json()["items"]) == 5


def test_a_hash_the_ledger_does_not_know_is_never_published(tmp_path: Path) -> None:
    ledger = FakeLedger()
    ghost = tx_hash("never-landed")
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("authorize_unknown", ghost, stage="authorize")])
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_NOT_SUCCESS
    assert out.json()["items"] == []
    assert "NOT_FOUND" in out.sheet() and ghost not in out.description()


def test_a_malformed_hash_is_never_asked_or_published(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("settle", "abc123")])
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_NOT_SUCCESS
    assert out.json()["items"] == []
    assert "MALFORMED" in out.sheet() and "not 64 hex" in out.sheet()
    assert "rpc getTransaction" not in ledger.calls


def test_an_unreadable_hash_is_unverified_not_failed(tmp_path: Path) -> None:
    ledger = FakeLedger(rpc_down=True, horizon_down=True)
    h = ledger.add("x")
    out = run(ledger, tmp_path, write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("seal", h)]))
    assert out.code == EXIT_UNREADABLE
    assert out.json()["items"] == []
    assert "UNREADABLE" in out.sheet()


def test_a_contradiction_outranks_an_unreadable_read(tmp_path: Path) -> None:
    ledger = FakeLedger(horizon_down=True)
    bad = ledger.add("bad", status="FAILED")  # the RPC answers FAILED
    unread = ledger.add("unread", rpc=False)  # past the RPC's window, and Horizon is down
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("seal", bad), row("settle", unread)])
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_NOT_SUCCESS
    assert "FAILED (rpc)" in out.sheet() and "UNREADABLE" in out.sheet()


# ── the network ─────────────────────────────────────────────────
def test_a_row_from_another_network_is_refused(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, _ = full_run(ledger, tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[3]["network"] = "mainnet"
    write_jsonl(path, rows)
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_REFUSED
    assert "network 'mainnet'" in out.out and "lifecycle.jsonl:4" in out.out
    assert not out.dir.exists()
    assert ledger.calls == []


def test_a_note_from_another_network_is_refused_too(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, _ = full_run(ledger, tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["network"] = "public"
    write_jsonl(path, rows)
    assert run(ledger, tmp_path, path).code == EXIT_REFUSED


def test_a_transaction_row_that_names_no_network_is_refused(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("settle", h, network="unknown")])
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_REFUSED and "not testnet" in out.out


@pytest.mark.parametrize("where", ["rpc", "horizon"])
def test_a_mainnet_ledger_is_refused(tmp_path: Path, where: str) -> None:
    ledger = FakeLedger()
    setattr(ledger, f"{where}_passphrase", MAINNET_PASSPHRASE)
    path, _ = full_run(ledger, tmp_path)
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_REFUSED and "testnet only" in out.out
    assert not out.dir.exists()


# ── input ───────────────────────────────────────────────────────
def test_a_torn_last_line_is_skipped(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("seal", h)], torn_tail='{"stage": "ver')
    out = run(ledger, tmp_path, path)
    assert out.code == EXIT_OK
    assert "Skipped 1 unreadable line(s)" in out.sheet()


def test_no_transaction_rows_is_refused(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("preflight", None, network="unknown")])
    out = run(FakeLedger(), tmp_path, path)
    assert out.code == EXIT_REFUSED and "no transaction rows" in out.out


def test_a_missing_input_is_refused(tmp_path: Path) -> None:
    out = run(FakeLedger(), tmp_path, tmp_path / "nowhere")
    assert out.code == EXIT_REFUSED and "no lifecycle.jsonl" in out.out


def test_disclosures_join_the_limitations(tmp_path: Path) -> None:
    ledger = FakeLedger()
    path, _ = full_run(ledger, tmp_path)
    text = run(ledger, tmp_path, path, extra=("--disclose", "The operator shown is a team wallet.")).description()
    assert "not been externally audited. The operator shown is a team wallet." in text


def test_secret_shaped_text_never_reaches_an_output(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    seed = "S" + "C" * 55
    bad = row("seal", h, agent=seed)
    path = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [bad])
    out = run(ledger, tmp_path, path)
    for text in (out.out, out.sheet(), out.description(), json.dumps(out.json())):
        assert seed not in text
