"""The evidence tool pinned against the lifecycle harness that writes its input (story 5.04).

The evidence tool imports nothing from `scripts/lifecycle/` at runtime; it
reads the JSONL the harness leaves on disk. So the row shape and the event
vocabulary it relies on are pinned here against the harness's own writer and
its own stages: a harness change that renamed a field or added a transaction
event fails in CI rather than dropping a hash from the video's evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.demo_evidence import config
from scripts.demo_evidence.rows import load
from scripts.lifecycle import evidence as harness_evidence
from scripts.lifecycle.evidence import EvidenceLog, EvidenceRow, tx_row, utc_now

ROOT = Path(__file__).resolve().parents[1]
H = "ab" * 32


def test_rows_the_harness_writes_are_read_field_for_field(tmp_path: Path) -> None:
    log = EvidenceLog(tmp_path)
    log.append(EvidenceRow(stage="preflight", event="preflight", utc=utc_now(), network="testnet", run_id="r1"))
    log.append(
        tx_row(
            stage="verify",
            event="settle",
            network="testnet",
            run_id="r1",
            tx_hash=H,
            onchain_status="SUCCESS",
            contract="CESCROW",
            agent="alpha",
            amount="0.0100000",
            asset="XLM",
            detail={"summary": "PaymentEscrow v2 settle"},
        )
    )
    loaded = load([tmp_path])  # the evidence directory, as the harness lays it out
    assert loaded.files == [tmp_path / harness_evidence.JSONL_NAME]
    (row,) = loaded.rows
    assert (row.stage, row.event, row.tx_hash, row.agent, row.amount, row.asset) == (
        "verify",
        "settle",
        H,
        "alpha",
        "0.0100000",
        "XLM",
    )
    assert (row.recorded_status, row.run_id, row.seq, row.summary) == ("SUCCESS", "r1", 2, "PaymentEscrow v2 settle")
    assert (row.kind, row.deliverable) == ("settle", "D4")


def test_the_file_name_is_the_harnesss() -> None:
    assert config.JSONL_NAME == harness_evidence.JSONL_NAME


def test_the_explorer_link_is_the_harnesss() -> None:
    assert config.EXPLORER_TX.format(H) == harness_evidence.EXPLORER_TX.format(H)


def _harness_tx_events() -> set[str]:
    """Every `event` the harness can file a transaction row under, read from its stages."""
    source = (ROOT / "scripts" / "lifecycle" / "stages.py").read_text()
    events: set[str] = set()
    for call in re.finditer(r"self\.tx\(\s*\"[a-z_]+\",\s*([^\n]+)", source):
        events.update(re.findall(r"\"([a-z_]+)\"", call.group(1)))
    return events


def test_every_transaction_event_the_harness_writes_has_a_kind() -> None:
    events = _harness_tx_events()
    assert {"authorize", "settle", "charge", "seal", "rating", "refund", "dispute_rating"} <= events
    unmapped = events - set(config.EVENT_KINDS)
    assert not unmapped, f"harness tx events with no evidence kind: {unmapped}"


def test_the_frozen_vocabulary() -> None:
    assert config.KINDS == ("register", "authorize", "settle", "seal", "rating", "dispute_rating", "refund", "other")
    assert set(config.KIND_DELIVERABLE) == set(config.KINDS)
    assert set(config.KIND_DELIVERABLE.values()) <= set(config.DELIVERABLES)
    assert set(config.EVENT_KINDS.values()) <= set(config.KINDS)
    assert set(config.KIND_LABEL) == set(config.KINDS) == set(config.KIND_PROVES)


def test_nothing_imports_the_harness_or_app_at_runtime() -> None:
    for source in (ROOT / "scripts" / "demo_evidence").glob("*.py"):
        text = source.read_text()
        assert not re.search(r"^\s*(from app\b|import app\b)", text, re.MULTILINE), source
        assert "scripts.lifecycle" not in text and "from ..lifecycle" not in text, source
