"""The description's limitations paragraph, derived from what the run shows.

Every sentence that is not true of every run is written only when the run
supports it: an uphold only beside a verified refund, a dispute rating only
beside a verified dispute rating, an open dispute only when the harness last
recorded it open. The strings are pinned whole, so a sentence that drifts
from what the evidence can support fails here rather than in a published
video description.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_demo_evidence_run import run

from scripts.demo_evidence.config import EXIT_NOT_SUCCESS, EXIT_OK
from scripts.demo_evidence.fakes import FakeLedger, row, write_jsonl

TESTNET = "Everything in this video runs on the Stellar TESTNET (SOW §3.6); no real value moved."
ASSET = (
    "On testnet the escrow's asset contract wraps native XLM, so every amount is testnet XLM, and the interface "
    'labels it XLM; the "usdc" in some API field names is the field\'s name, not the asset.'
)
UPHELD = (
    "A dispute was upheld by the platform's adjudicator key — a human decision behind an API key, not an on-chain "
    "arbiter — and its refund is a partial credit paid from the platform's signing key, which is also escrow v2's "
    "settler, not a clawback from the operator."
)
RATED_AFTER_REFUND = "The dispute's rating then landed on the ReputationLedger."
NOT_RATED_AFTER_REFUND = "No dispute rating is among the verified transactions, so the video claims none."
RATED_ONLY = (
    "A dispute rating landed on the ReputationLedger, but no refund is among the verified transactions, so the "
    "video claims none."
)
PRIOR = "Reputation scores are prior-smoothed, so an agent's lower bound near the floor can move with a single rating."
AUDIT = "The contracts have not been externally audited."

DISPUTE = "dsp_15acee279ac02852a5877ac1696ec4b5"


def paragraph(*dispute: str) -> str:
    return "Limitations. " + " ".join((TESTNET, ASSET, *dispute, PRIOR, AUDIT))


def limitations(text: str) -> str:
    (line,) = [line for line in text.splitlines() if line.startswith("Limitations. ")]
    return line


def settled_run(ledger: FakeLedger) -> list[dict[str, Any]]:
    """A run with no dispute: authorize, settle, seal and the run's rating."""
    return [
        row("authorize", ledger.add("authorize"), seq=1, amount="0.0200000"),
        row("settle", ledger.add("settle"), stage="verify", seq=2, amount="0.0100000"),
        row("seal", ledger.add("seal"), stage="verify", seq=3),
        row("rating", ledger.add("rating"), stage="poll", seq=4),
    ]


def note(event: str, stage: str, seq: int, **detail: Any) -> dict[str, Any]:
    """A harness note (no transaction) carrying `detail`, as `Runner.note` writes it."""
    out = row(event, None, stage=stage, seq=seq)
    out["detail"] = {"summary": f"{event} row", **detail}
    return out


def described(ledger: FakeLedger, tmp_path: Path, rows: list[dict[str, Any]], code: int = EXIT_OK) -> str:
    out = run(ledger, tmp_path, write_jsonl(tmp_path / "take" / "lifecycle.jsonl", rows))
    assert out.code == code, out.out
    return limitations(out.description())


# ── no dispute ──────────────────────────────────────────────────
def test_a_run_with_no_dispute_claims_no_uphold_and_no_refund(tmp_path: Path) -> None:
    ledger = FakeLedger()
    text = described(ledger, tmp_path, settled_run(ledger))
    assert text == paragraph()
    for claim in ("upheld", "adjudicat", "refund", "dispute", "settler"):
        assert claim not in text.lower(), claim


# ── a dispute rating alone ──────────────────────────────────────
def test_a_rating_only_dispute_claims_the_rating_and_no_refund(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [*settled_run(ledger), row("dispute_rating", ledger.add("dispute"), stage="refund", seq=5)]
    text = described(ledger, tmp_path, rows)
    assert text == paragraph(RATED_ONLY)
    assert "upheld" not in text and "partial credit" not in text


def test_a_refund_that_failed_on_the_ledger_is_not_claimed(tmp_path: Path) -> None:
    """Derived from the VERIFIED rows: a refund row the ledger reads FAILED is no refund."""
    ledger = FakeLedger()
    refund = ledger.add("refund")
    ledger.txs[refund].status = "FAILED"
    rows = [
        *settled_run(ledger),
        row("refund", refund, stage="refund", seq=5, amount="0.0050000"),
        row("dispute_rating", ledger.add("dispute"), stage="refund", seq=6),
    ]
    assert described(ledger, tmp_path, rows, code=EXIT_NOT_SUCCESS) == paragraph(RATED_ONLY)


# ── an uphold, shown by its refund ──────────────────────────────
def test_an_upheld_dispute_with_its_refund_and_rating_claims_both(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [
        *settled_run(ledger),
        row("refund", ledger.add("refund"), stage="refund", seq=5, amount="0.0050000"),
        row("dispute_rating", ledger.add("dispute"), stage="refund", seq=6),
    ]
    assert described(ledger, tmp_path, rows) == paragraph(UPHELD, RATED_AFTER_REFUND)


def test_a_refund_whose_rating_did_not_land_claims_no_rating(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [*settled_run(ledger), row("refund", ledger.add("refund"), stage="refund", seq=5, amount="0.0050000")]
    assert described(ledger, tmp_path, rows) == paragraph(UPHELD, NOT_RATED_AFTER_REFUND)


# ── an open dispute, from the harness's record ──────────────────
def test_a_dispute_the_harness_left_open_is_said_to_be_open(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [*settled_run(ledger), note("dispute_opened", "dispute", 5, dispute_id=DISPUTE, status="open")]
    text = described(ledger, tmp_path, rows)
    assert text == paragraph(
        f"Dispute {DISPUTE} was open when the run was recorded: no one had adjudicated it, so the video claims no "
        "refund and no dispute rating for it."
    )
    assert "upheld" not in text


def test_the_2026_09_30_run_whose_note_has_the_status_only_in_its_summary_is_open(tmp_path: Path) -> None:
    """The committed v2 run (h3): its `dispute_opened` note predates `detail.status`."""
    ledger = FakeLedger()
    opened = note(
        "dispute_opened",
        "dispute",
        5,
        charged_usdc=0.01,
        creditable_usdc=0.01,
        dispute_id=DISPUTE,
        job_id_hex="dd9089ab7791c4293baf87745d1ea0b6",
        step_index=0,
    )
    opened["detail"]["summary"] = f"dispute {DISPUTE} on step 0 (calculatorai), status open"
    text = described(ledger, tmp_path, [*settled_run(ledger), opened])
    assert f"Dispute {DISPUTE} was open when the run was recorded" in text
    assert "upheld" not in text


def test_two_open_disputes_are_named_together(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [
        *settled_run(ledger),
        note("dispute_opened", "dispute", 5, dispute_id="dsp_a", status="open"),
        note("dispute_existing", "dispute", 6, dispute_id="dsp_b", status="open"),
    ]
    assert described(ledger, tmp_path, rows) == paragraph(
        "Disputes dsp_a, dsp_b were open when the run was recorded: no one had adjudicated them, so the video "
        "claims no refund and no dispute rating for them."
    )


def test_a_dispute_upheld_later_in_the_run_is_not_said_to_be_open(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [
        *settled_run(ledger),
        note("dispute_opened", "dispute", 5, dispute_id=DISPUTE, status="open"),
        note("upheld", "uphold", 6, dispute_id=DISPUTE, status="credited"),
        row("refund", ledger.add("refund"), stage="refund", seq=7, amount="0.0050000"),
        row("dispute_rating", ledger.add("dispute"), stage="refund", seq=8),
    ]
    assert described(ledger, tmp_path, rows) == paragraph(UPHELD, RATED_AFTER_REFUND)


def test_a_later_note_without_a_status_leaves_the_dispute_unknown_not_open(tmp_path: Path) -> None:
    """`uphold_skipped` names the dispute and no status: the tool no longer knows it is open, so it says nothing."""
    ledger = FakeLedger()
    rows = [
        *settled_run(ledger),
        note("dispute_opened", "dispute", 5, dispute_id=DISPUTE, status="open"),
        note("uphold_skipped", "uphold", 6, dispute_id=DISPUTE),
    ]
    assert described(ledger, tmp_path, rows) == paragraph()


# ── the asset ───────────────────────────────────────────────────
def test_the_asset_sentence_says_testnet_xlm_labelled_xlm(tmp_path: Path) -> None:
    ledger = FakeLedger()
    text = described(ledger, tmp_path, settled_run(ledger))
    assert ASSET in text
    assert "labels USDC" not in text and "interface labels it XLM" in text


# ── the sheet's "what it proves" ────────────────────────────────
def test_the_sheet_claims_no_uphold_for_a_dispute_rating_or_a_refund(tmp_path: Path) -> None:
    ledger = FakeLedger()
    rows = [
        row("refund", ledger.add("refund"), stage="refund", seq=1, amount="0.0050000"),
        row("dispute_rating", ledger.add("dispute"), stage="refund", seq=2),
    ]
    out = run(ledger, tmp_path, write_jsonl(tmp_path / "take" / "lifecycle.jsonl", rows))
    sheet = out.sheet()
    assert "| the platform's signing key paid the buyer a partial credit for the disputed step |" in sheet
    assert "| a kind=dispute rating against the disputed agent landed on the ReputationLedger |" in sheet
    assert "upheld" not in sheet and "settler" not in sheet
