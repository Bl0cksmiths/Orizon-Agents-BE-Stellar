"""The index-links output: verified hashes in the frontend evidence index's link shape (story 5.01's rule).

`--index-links` hands every verified transaction to the evidence index
(`content/evidence/index.json`) as a link the index's validator accepts,
grouped by the index item it is evidence for. Pinned here: the exact shape,
the date (the UTC date of the ledger's `created_at` on Horizon, not the run's
clock), plain-language labels, and the kind → item grouping.
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
from scripts.demo_evidence.config import EXIT_NOT_SUCCESS, EXIT_OK, EXIT_UNREADABLE, KIND_INDEX_ITEM, KINDS
from scripts.demo_evidence.fakes import HORIZON, RPC, FakeLedger, row, write_jsonl
from scripts.demo_evidence.links import label_problem, utc_date

ADDRESS = "GBI2I" + "A" * 47 + "ADBH"


@dataclass
class Outcome:
    code: int
    out: str
    path: Path

    def body(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded

    def links(self) -> list[dict[str, Any]]:
        return [link for item in self.body()["items"] for link in item["links"]]


def run(ledger: FakeLedger, tmp_path: Path, *args: str, index: bool = True) -> Outcome:
    stream = io.StringIO()
    path = tmp_path / "index-links.json"
    code = main(
        [
            *args,
            "--out-dir",
            str(tmp_path / "video"),
            "--rpc-url",
            RPC,
            "--horizon-url",
            HORIZON,
            *(["--index-links", str(path)] if index else []),
        ],
        transport=ledger.transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_000.0,  # 2026-09-21 UTC: never the ledger's date below
    )
    return Outcome(code, stream.getvalue(), path)


def test_the_shape_is_the_evidence_indexs_link_pinned_exactly(tmp_path: Path) -> None:
    ledger = FakeLedger()
    settle = ledger.add("settle", created_at="2026-09-24T12:00:00Z")
    reg = ledger.add("reg", created_at="2026-09-23T08:15:00Z")
    harness = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("settle", settle, amount="0.0100000")])
    out = run(ledger, tmp_path, str(harness), "--tx", f"register={reg}:Operator registers research.pro")
    assert out.code == EXIT_OK, out.out
    assert out.body() == {
        "schema": "orizon.evidence-index-links/1",
        "network": "testnet",
        "generated_at": 1_790_000_000,
        "items": [
            {
                "id": "6.1-D1-c",
                "links": [
                    {
                        "label": "Operator registers research.pro — 2026-09-23",
                        "url": f"https://stellar.expert/explorer/testnet/tx/{reg}",
                        "kind": "tx",
                        "tx_hash": reg,
                        "date": "2026-09-23",
                    }
                ],
            },
            {
                "id": "6.1-D4-d",
                "links": [
                    {
                        "label": "Settlement for agent alpha of 0.0100000 XLM on Stellar testnet — 2026-09-24",
                        "url": f"https://stellar.expert/explorer/testnet/tx/{settle}",
                        "kind": "tx",
                        "tx_hash": settle,
                        "date": "2026-09-24",
                    }
                ],
            },
        ],
    }
    for link in out.links():
        assert list(link) == ["label", "url", "kind", "tx_hash", "date"]  # the index's own key order


def test_the_date_is_the_ledgers_created_at_on_horizon_in_utc(tmp_path: Path) -> None:
    ledger = FakeLedger()
    late = ledger.add("late", created_at="2026-09-24T23:59:59Z")
    offset = ledger.add("offset", created_at="2026-09-25T02:00:00+05:00", rpc=False)
    out = run(ledger, tmp_path, "--tx", f"settle={late}", "--tx", f"seal={offset}")
    assert out.code == EXIT_OK, out.out
    assert [(link["tx_hash"], link["date"]) for link in out.links()] == [(late, "2026-09-24"), (offset, "2026-09-24")]
    # The RPC verified `late`, so its date was asked of Horizon; `offset` was verified there already.
    assert ledger.calls.count(f"horizon GET /transactions/{late}") == 1
    assert ledger.calls.count(f"horizon GET /transactions/{offset}") == 1


def test_a_hash_whose_date_cannot_be_read_gets_no_link_and_says_rerun(tmp_path: Path) -> None:
    ledger = FakeLedger(horizon_down=True)
    h = ledger.add("x")  # the RPC verifies it; Horizon, which dates it, is down
    out = run(ledger, tmp_path, "--tx", f"settle={h}")
    assert out.code == EXIT_UNREADABLE
    assert out.body()["items"] == []
    assert "ledger date could not be read" in out.out
    items = json.loads((tmp_path / "video" / "evidence.json").read_text())["items"]
    assert [i["tx_hash"] for i in items] == [h]  # it verified, so it is still published


def test_a_created_at_that_is_not_a_timestamp_gets_no_link(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x", created_at="yesterday")
    out = run(ledger, tmp_path, "--tx", f"settle={h}")
    assert out.code == EXIT_UNREADABLE
    assert out.body()["items"] == [] and "no ledger date" in out.out


def test_only_verified_hashes_become_links(tmp_path: Path) -> None:
    ledger = FakeLedger()
    ok, bad = ledger.add("ok"), ledger.add("bad", status="FAILED")
    out = run(ledger, tmp_path, "--tx", f"settle={ok}", "--tx", f"refund={bad}", "--tx", "seal=abc")
    assert out.code == EXIT_NOT_SUCCESS
    assert [link["tx_hash"] for link in out.links()] == [ok]
    assert bad not in out.path.read_text()


def test_without_the_flag_nothing_is_written_and_nothing_extra_is_read(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    out = run(ledger, tmp_path, "--tx", f"settle={h}", index=False)
    assert out.code == EXIT_OK
    assert not out.path.exists()
    assert not any(c.startswith("horizon GET /transactions/") for c in ledger.calls)


# ── the grouping ────────────────────────────────────────────────
def test_every_kind_lands_in_its_index_item_in_the_indexs_order(tmp_path: Path) -> None:
    ledger = FakeLedger()
    hashes = {kind: ledger.add(kind) for kind in reversed(KINDS)}
    args = [a for kind, h in hashes.items() for a in ("--tx", f"{kind}={h}")]
    out = run(ledger, tmp_path, *args)
    assert out.code == EXIT_OK, out.out
    grouped = {item["id"]: [link["tx_hash"] for link in item["links"]] for item in out.body()["items"]}
    assert list(grouped) == ["6.1-D1-c", "6.1-D2-a", "6.1-D3-a", "6.1-D3-b", "6.1-D4-d", "6.1-RD-f"]
    assert grouped == {
        "6.1-D1-c": [hashes["register"]],
        "6.1-D2-a": [hashes["rating"]],
        "6.1-D3-a": [hashes["dispute_rating"]],
        "6.1-D3-b": [hashes["refund"]],
        # Given in reverse, so the sheet's order is seal, settle, authorize.
        "6.1-D4-d": [hashes["seal"], hashes["settle"], hashes["authorize"]],
        "6.1-RD-f": [hashes["other"]],
    }


def test_the_grouping_table() -> None:
    assert KIND_INDEX_ITEM == {
        "register": "6.1-D1-c",
        "rating": "6.1-D2-a",
        "dispute_rating": "6.1-D3-a",
        "refund": "6.1-D3-b",
        "authorize": "6.1-D4-d",
        "settle": "6.1-D4-d",
        "seal": "6.1-D4-d",
        "other": "6.1-RD-f",
    }


def test_harness_events_group_by_their_kind(tmp_path: Path) -> None:
    ledger = FakeLedger()
    charge, unknown = ledger.add("charge"), ledger.add("unknown")
    harness = write_jsonl(
        tmp_path / "t" / "lifecycle.jsonl",
        [row("charge", charge, seq=1), row("authorize_unknown", unknown, stage="authorize", seq=2)],
    )
    out = run(ledger, tmp_path, str(harness))
    assert [(item["id"], len(item["links"])) for item in out.body()["items"]] == [("6.1-D4-d", 2)]


# ── plain labels ────────────────────────────────────────────────
def test_every_generated_label_is_plain_language(tmp_path: Path) -> None:
    ledger = FakeLedger()
    hashes = {kind: ledger.add(kind) for kind in KINDS}
    out = run(ledger, tmp_path, *[a for kind, h in hashes.items() for a in ("--tx", f"{kind}={h}")])
    assert out.code == EXIT_OK, out.out
    labels = [link["label"] for link in out.links()]
    assert len(labels) == len(KINDS)
    for label in labels:
        assert label_problem(label) is None, label
        assert not re.search(r"[0-9a-f]{64}", label), label
        assert label.endswith(" — 2026-09-24"), label
    assert "Transaction on Stellar testnet — 2026-09-24" in labels  # `other`'s one-word kind label, made two


def test_a_full_address_or_hash_in_a_label_is_shortened(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    harness = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("register", h, agent=ADDRESS)])
    given = ledger.add("y")
    out = run(ledger, tmp_path, str(harness), "--tx", f"settle={given}:Settlement repeating {given}")
    labels = [link["label"] for link in out.links()]
    assert labels == [
        "Agent registration for agent GBI2I…ADBH on Stellar testnet — 2026-09-24",
        f"Settlement repeating {given[:8]}…{given[-8:]} — 2026-09-24",
    ]
    assert ADDRESS not in out.path.read_text()


def test_a_secret_shaped_agent_never_reaches_the_links(tmp_path: Path) -> None:
    ledger = FakeLedger()
    h = ledger.add("x")
    seed = "S" + "C" * 55
    harness = write_jsonl(tmp_path / "t" / "lifecycle.jsonl", [row("seal", h, agent=seed)])
    out = run(ledger, tmp_path, str(harness))
    assert seed not in out.path.read_text()


# The frontend validator's own cases (`lib/evidence/validate.test.ts`).
@pytest.mark.parametrize(
    "label",
    [
        "a" * 64,
        "9b8ffaa4…8f919a68",
        "0xdeadbeefcafe",
        "G" + "A" * 55,
        "C" + "B" * 55,
        "GABC…WXYZ",
        "CAPH...J3GQ",
    ],
)
def test_the_label_rule_refuses_a_bare_id(label: str) -> None:
    assert label_problem(label) == "is a bare hash or address; say in words what the link shows"


# "Settlement x": a word is two letters in a row, so a lone letter is not one.
@pytest.mark.parametrize("label", ["Link", "Tx 9b8ffaa4…8f919a68", "Account G" + "A" * 55, "Settlement x"])
def test_the_label_rule_refuses_one_word(label: str) -> None:
    assert label_problem(label) == "must be at least two words of plain language, not just an id"


def test_the_label_rule_refuses_nothing() -> None:
    assert label_problem("  ") == "must be a non-empty string"


@pytest.mark.parametrize("label", ["Registration by outside operator GABC…WXYZ", "Merged PR #42"])
def test_the_label_rule_accepts_words_that_quote_an_id(label: str) -> None:
    assert label_problem(label) is None


@pytest.mark.parametrize(
    ("created_at", "date"),
    [
        ("2026-09-24T12:00:00Z", "2026-09-24"),
        ("2026-09-24T23:59:59Z", "2026-09-24"),
        ("2026-09-25T02:00:00+05:00", "2026-09-24"),
        ("2026-09-24T12:00:00", None),  # no zone: not guessed
        ("not a date", None),
    ],
)
def test_utc_date(created_at: str, date: str | None) -> None:
    assert utc_date(created_at) == date
