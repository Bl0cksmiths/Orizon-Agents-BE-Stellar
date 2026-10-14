"""Characterization tests for `rows.load`'s line handling.

Blank lines are ignored, a line that is not a JSON object is skipped and
counted, an unreadable file is an input error, and the network checks run in
order: a row naming another network stops the run even when it carries no
hash, and a hash row must name testnet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.demo_evidence.fakes import row, tx_hash
from scripts.demo_evidence.rows import InputError, WrongNetwork, load


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_blank_lines_are_ignored_and_non_objects_are_skipped(tmp_path: Path) -> None:
    h1, h2 = tx_hash("one"), tx_hash("two")
    path = _write(
        tmp_path / "lifecycle.jsonl",
        [
            "",
            json.dumps(row("settle", h1.upper() + "  ", seq=4)),
            "   ",
            "[1, 2]",
            '"a string"',
            "{torn",
            json.dumps(row("refund", h2, seq="5", agent=None, amount="0.5")),
            json.dumps(row("settle", h1)),
        ],
    )
    loaded = load([path])
    assert (loaded.skipped_lines, loaded.duplicates) == (3, 1)
    first, second = loaded.rows
    assert (first.source, first.tx_hash, first.seq) == (f"{path}:2", h1, 4)
    assert (second.source, second.seq, second.agent, second.amount, second.asset) == (
        f"{path}:7",
        None,
        None,
        "0.5",
        "XLM",
    )


def test_an_unreadable_file_is_an_input_error(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(InputError, match=rf"^{path}: cannot be read: "):
        load([path])


def test_a_row_naming_another_network_stops_the_run_even_without_a_hash(tmp_path: Path) -> None:
    path = _write(tmp_path / "lifecycle.jsonl", [json.dumps(row("note", None, network="mainnet"))])
    with pytest.raises(WrongNetwork, match=r":1: a row from network 'mainnet'; the video's evidence is testnet only$"):
        load([path])


@pytest.mark.parametrize(("network", "shown"), [("unknown", "'unknown'"), ("", "'unset'")])
def test_a_hash_row_must_name_testnet(tmp_path: Path, network: str, shown: str) -> None:
    path = _write(tmp_path / "lifecycle.jsonl", [json.dumps(row("settle", tx_hash("x"), network=network))])
    with pytest.raises(WrongNetwork, match=rf":1: a transaction row whose network is {shown}, not testnet; "):
        load([path])


def test_a_note_without_a_network_is_context_not_a_refusal(tmp_path: Path) -> None:
    lines = [json.dumps(row("note", None, network="unknown")), json.dumps(row("settle", tx_hash("y")))]
    loaded = load([_write(tmp_path / "lifecycle.jsonl", lines)])
    assert [r.tx_hash for r in loaded.rows] == [tx_hash("y")]
