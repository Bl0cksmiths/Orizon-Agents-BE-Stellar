"""Characterization tests for `uphold_dispute.report` when the dispute is gone.

A dispute missing from the store after the uphold call says nothing about the
money, so the report sends the operator to the chain, and never exits 0: a
clean call becomes EXIT_UNEXPECTED, and a refusal keeps its own code. Nothing
here reaches a network; `report` only reads the record it is handed.
"""

from __future__ import annotations

import pytest

from scripts import uphold_dispute

MISSING = [
    "",
    "  dispute dsp_gone is not in the store after the uphold call.",
    "  Check the payer's account on-chain before doing anything else.",
    "",
]


@pytest.mark.parametrize(
    ("fallback", "code"),
    [
        (uphold_dispute.EXIT_OK, uphold_dispute.EXIT_UNEXPECTED),
        (uphold_dispute.EXIT_TRANSFER_FAILED, uphold_dispute.EXIT_TRANSFER_FAILED),
        (uphold_dispute.EXIT_TIMEOUT, uphold_dispute.EXIT_TIMEOUT),
    ],
)
@pytest.mark.parametrize("amount", [None, 0.07])
def test_a_dispute_missing_after_the_uphold_sends_the_operator_to_the_chain(
    capsys: pytest.CaptureFixture[str], fallback: int, code: int, amount: float | None
) -> None:
    assert uphold_dispute.report(None, "dsp_gone", amount, fallback) == code
    assert capsys.readouterr().out.splitlines() == MISSING
