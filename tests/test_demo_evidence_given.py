"""Hashes the browser takes produced, given with `--tx` and `--rows` (story 5.04 AC3).

The video's register, authorize, settle, dispute and refund are driven in the
browser, so their hashes never pass through the lifecycle harness. The rule
under test is the harness rows' rule: a given hash is re-verified on testnet
exactly like one of theirs, only SUCCESS is published, and a given row that
names another network or an unknown kind stops the run before anything is
read or written.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.demo_evidence.cli import main
from scripts.demo_evidence.config import EXIT_NOT_SUCCESS, EXIT_OK, EXIT_REFUSED
from scripts.demo_evidence.fakes import HORIZON, MAINNET_PASSPHRASE, RPC, FakeLedger, row, tx_hash, write_jsonl


@dataclass
class Outcome:
    code: int
    out: str
    dir: Path

    def items(self) -> list[dict[str, Any]]:
        body: dict[str, Any] = json.loads((self.dir / "evidence.json").read_text())
        return list(body["items"])

    def sheet(self) -> str:
        return (self.dir / "evidence-sheet.md").read_text()

    def description(self) -> str:
        return (self.dir / "description.txt").read_text()


def run(ledger: FakeLedger, tmp_path: Path, *args: str) -> Outcome:
    stream = io.StringIO()
    out_dir = tmp_path / "video"
    code = main(
        [*args, "--out-dir", str(out_dir), "--rpc-url", RPC, "--horizon-url", HORIZON],
        transport=ledger.transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_000.0,
    )
    return Outcome(code, stream.getvalue(), out_dir)


def rows_file(tmp_path: Path, body: Any, name: str = "browser.json") -> Path:
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


# ── verified ────────────────────────────────────────────────────
def test_a_browser_registration_given_with_tx_is_verified_and_published(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("register")
    out = run(ledger, tmp_path, "--tx", f"register={h}:Operator registers research.pro")
    assert out.code == EXIT_OK, out.out
    assert out.items() == [
        {
            "label": "Operator registers research.pro",
            "deliverable": "D1",
            "kind": "register",
            "tx_hash": h,
            "explorer": f"https://stellar.expert/explorer/testnet/tx/{h}",
            "verified": True,
        }
    ]
    assert "rpc getTransaction" in ledger.calls
    assert "| browser · Operator registers research.pro |" in out.sheet()
    assert "1 hash(es) given with `--tx`" in out.sheet()
    assert h in out.description()


def test_a_tx_without_a_label_takes_its_kinds(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("settle")
    out = run(ledger, tmp_path, "--tx", f"SETTLE={h.upper()}")
    assert out.code == EXIT_OK, out.out
    (item,) = out.items()
    assert (item["label"], item["kind"], item["deliverable"], item["tx_hash"]) == ("Settlement", "settle", "D4", h)


def test_a_stellar_expert_testnet_link_is_taken_as_its_hash(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("refund")
    link = f"https://stellar.expert/explorer/testnet/tx/{h}"
    out = run(ledger, tmp_path, "--tx", f"refund={link}:Partial credit paid to the buyer")
    assert out.code == EXIT_OK, out.out
    (item,) = out.items()
    assert (item["tx_hash"], item["label"], item["deliverable"]) == (h, "Partial credit paid to the buyer", "D3")


def test_a_rows_file_is_verified_like_harness_rows(tmp_path: Path) -> None:
    ledger = FakeLedger()
    reg, auth, other = ledger.add("reg"), ledger.add("auth", rpc=False), ledger.add("other")
    path = rows_file(
        tmp_path,
        [
            {"kind": "register", "tx_hash": reg, "label": "Operator registers research.pro", "deliverable": "D1"},
            {"kind": "authorize", "tx_hash": auth, "network": "testnet"},
            {"kind": "other", "tx_hash": other, "label": "Settler role handed over", "deliverable": "D2"},
        ],
    )
    out = run(ledger, tmp_path, "--rows", str(path))
    assert out.code == EXIT_OK, out.out
    assert [(i["kind"], i["deliverable"], i["tx_hash"]) for i in out.items()] == [
        ("register", "D1", reg),
        ("authorize", "D4", auth),
        ("other", "D2", other),
    ]
    assert f"horizon GET /transactions/{auth}" in ledger.calls  # past the RPC window: Horizon verified it
    assert f"`{path}`" in out.sheet()


# ── not SUCCESS: listed, never published ────────────────────────
def test_a_failed_given_hash_is_never_published(tmp_path: Path) -> None:
    ledger = FakeLedger()
    ok, bad = ledger.add("ok"), ledger.add("bad", status="FAILED")
    out = run(ledger, tmp_path, "--tx", f"settle={ok}", "--tx", f"refund={bad}:Partial credit paid to the buyer")
    assert out.code == EXIT_NOT_SUCCESS
    assert [i["tx_hash"] for i in out.items()] == [ok]
    assert bad not in out.description()
    assert f"`{bad}`" in out.sheet().split("## Failed verification")[1]


def test_a_given_hash_the_ledger_does_not_know_is_never_published(tmp_path: Path) -> None:
    ledger = FakeLedger()
    ghost = tx_hash("never-landed")
    out = run(ledger, tmp_path, "--rows", str(rows_file(tmp_path, [{"kind": "settle", "tx_hash": ghost}])))
    assert out.code == EXIT_NOT_SUCCESS
    assert out.items() == []
    assert "NOT_FOUND" in out.sheet() and ghost not in out.description()


def test_a_malformed_given_hash_is_listed_and_never_asked(tmp_path: Path) -> None:
    ledger = FakeLedger()
    out = run(ledger, tmp_path, "--tx", "seal=abc123")
    assert out.code == EXIT_NOT_SUCCESS
    assert out.items() == []
    assert "MALFORMED" in out.sheet() and "not 64 hex" in out.sheet()
    assert "rpc getTransaction" not in ledger.calls


# ── refused: nothing read, nothing written ──────────────────────
@pytest.mark.parametrize(
    ("args", "says"),
    [
        (["--tx", "charge=HASH"], "kind 'charge' is not one of"),
        (["--tx", "authorize_unknown=HASH"], "kind 'authorize_unknown'"),
        (["--tx", "HASH"], "is not KIND=HASH[:label]"),
        (["--tx", "settle="], "no transaction hash"),
        (["--tx", "settle=HASH:Settlement"], "at least two words"),
        (["--tx", "settle=HASH:HASH"], "is a bare hash or address"),
        (["--tx", "settle=https://example.com/tx/HASH"], "not a Stellar Expert transaction link"),
    ],
)
def test_a_bad_tx_is_refused(tmp_path: Path, args: list[str], says: str) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    out = run(ledger, tmp_path, *(a.replace("HASH", h) for a in args))
    assert out.code == EXIT_REFUSED, out.out
    assert says in out.out
    assert not out.dir.exists() and ledger.calls == []


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ("{not json", "not JSON"),
        ({"kind": "settle", "tx_hash": "HASH"}, "must be a JSON list"),
        (["HASH"], "must be an object"),
        ([{"kind": "settle"}], "tx_hash must be a string"),
        ([{"kind": "bogus", "tx_hash": "HASH"}], "kind 'bogus' is not one of"),
        ([{"tx_hash": "HASH"}], "kind None is not one of"),
        ([{"kind": "settle", "tx_hash": "HASH", "stage": "x"}], "unknown key(s) stage"),
        ([{"kind": "settle", "tx_hash": "HASH", "deliverable": "D1"}], "a 'settle' is evidence for D4, not D1"),
        ([{"kind": "other", "tx_hash": "HASH", "deliverable": "D9"}], "deliverable 'D9' is not one of"),
        ([{"kind": "settle", "tx_hash": "HASH", "label": 7}], "label must be a string"),
        ([{"kind": "settle", "tx_hash": "HASH", "label": "GABC…WXYZ"}], "is a bare hash or address"),
    ],
)
def test_a_malformed_rows_file_is_refused(tmp_path: Path, body: Any, says: str) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    text = body if isinstance(body, str) else json.dumps(body).replace("HASH", h)
    out = run(ledger, tmp_path, "--rows", str(rows_file(tmp_path, text)))
    assert out.code == EXIT_REFUSED, out.out
    assert says in out.out
    assert not out.dir.exists() and ledger.calls == []


def test_an_unreadable_rows_file_is_refused(tmp_path: Path) -> None:
    out = run(FakeLedger(), tmp_path, "--rows", str(tmp_path / "nowhere.json"))
    assert out.code == EXIT_REFUSED and "cannot be read" in out.out


@pytest.mark.parametrize(
    "args",
    [
        ["--rows", "ROWS"],
        ["--tx", "settle=https://stellar.expert/explorer/public/tx/HASH"],
        ["--tx", "settle=https://stellar.expert/explorer/public/tx/HASH:Settlement of the step"],
    ],
)
def test_a_given_hash_from_another_network_is_refused(tmp_path: Path, args: list[str]) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    rows = rows_file(tmp_path, [{"kind": "settle", "tx_hash": h, "network": "public"}])
    out = run(ledger, tmp_path, *(a.replace("ROWS", str(rows)).replace("HASH", h) for a in args))
    assert out.code == EXIT_REFUSED, out.out
    assert "testnet only" in out.out and "'public'" in out.out
    assert not out.dir.exists() and ledger.calls == []


@pytest.mark.parametrize("where", ["rpc", "horizon"])
def test_given_hashes_on_a_mainnet_ledger_are_refused(tmp_path: Path, where: str) -> None:
    ledger = FakeLedger()
    setattr(ledger, f"{where}_passphrase", MAINNET_PASSPHRASE)
    out = run(ledger, tmp_path, "--tx", f"settle={ledger.add('x')}")
    assert out.code == EXIT_REFUSED and "testnet only" in out.out
    assert not out.dir.exists()
    assert "rpc getTransaction" not in ledger.calls


def test_nothing_to_read_is_refused(tmp_path: Path) -> None:
    out = run(FakeLedger(), tmp_path)
    assert out.code == EXIT_REFUSED and "nothing to read" in out.out


def test_an_empty_rows_file_alone_is_refused(tmp_path: Path) -> None:
    out = run(FakeLedger(), tmp_path, "--rows", str(rows_file(tmp_path, [])))
    assert out.code == EXIT_REFUSED and "no transaction rows" in out.out


# ── merged with the harness ─────────────────────────────────────
def test_given_hashes_follow_the_harness_rows_each_hash_once(tmp_path: Path) -> None:
    ledger = FakeLedger()
    authorize, settle, reg, refund, seal = (ledger.add(t) for t in ("a", "s", "r", "f", "e"))
    harness = write_jsonl(
        tmp_path / "take-1" / "lifecycle.jsonl", [row("authorize", authorize, seq=1), row("settle", settle, seq=2)]
    )
    rows = rows_file(
        tmp_path,
        [
            {"kind": "register", "tx_hash": reg, "label": "Operator registers research.pro"},
            {"kind": "settle", "tx_hash": settle, "label": "The same settlement, from the browser"},
        ],
    )
    out = run(
        ledger,
        tmp_path,
        str(harness.parent),
        "--tx",
        f"refund={refund}",
        "--rows",
        str(rows),
        "--tx",
        f"seal={seal}",
        "--tx",
        f"register={reg}",
    )
    assert out.code == EXIT_OK, out.out
    # The harness first, then the rows files, then --tx, each in the order given.
    assert [i["tx_hash"] for i in out.items()] == [authorize, settle, reg, refund, seal]
    # A repeated hash keeps its first appearance, and its label.
    assert out.items()[1]["label"] == "Settlement — alpha"
    assert "2 repeated hash(es)" in out.sheet()
    assert ledger.calls.count("rpc getTransaction") == 5


def test_a_hash_given_as_two_kinds_is_refused(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    harness = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("settle", h)])
    out = run(ledger, tmp_path, str(harness), "--tx", f"refund={h}")
    assert out.code == EXIT_REFUSED
    assert "is given as 'refund'" in out.out and "filed it as 'settle'" in out.out
    assert not out.dir.exists()


def test_a_harness_file_with_no_transactions_is_fine_beside_a_given_hash(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    harness = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("preflight", None, network="unknown")])
    out = run(ledger, tmp_path, str(harness), "--tx", f"register={h}:Operator registers research.pro")
    assert out.code == EXIT_OK, out.out
    assert [i["tx_hash"] for i in out.items()] == [h]


def test_a_harness_file_with_no_transactions_alone_is_still_refused(tmp_path: Path) -> None:
    harness = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("preflight", None, network="unknown")])
    out = run(FakeLedger(), tmp_path, str(harness))
    assert out.code == EXIT_REFUSED and "no transaction rows" in out.out
