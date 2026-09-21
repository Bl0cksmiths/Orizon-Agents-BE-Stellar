"""The per-step output summary a settlement keeps (story 4.05).

A dispute is about what a step produced, and the trace that showed it does not
survive the window — so the settlement carries the line. Pinned here: it
round-trips through the one JSON column, and a row written before the field
existed still reads, because those settlements are still inside their windows.
"""

from __future__ import annotations

import json

from app.services.dispute_store import (
    OUTPUT_SUMMARY_MAX_CHARS,
    SettlementStep,
    steps_from_json,
    steps_to_json,
)


def test_the_summary_survives_the_json_column() -> None:
    steps = (
        SettlementStep(0, "researcher", "Researcher", 0.05, True, "12 sources, 3 conflicting"),
        SettlementStep(1, "coder", "Coder", 0.08, False, None),
    )
    assert steps_from_json(steps_to_json(steps)) == steps


def test_a_settlement_recorded_before_the_summary_existed_still_reads() -> None:
    legacy = json.dumps(
        [{"step_index": 0, "agent_id": "researcher", "agent_name": "Researcher", "price_usdc": 0.05, "delivered": True}]
    )
    (step,) = steps_from_json(legacy)
    assert step.output_summary is None
    assert step.delivered is True


def test_a_step_built_without_a_summary_defaults_to_none() -> None:
    # Every caller that constructs a SettlementStep today, positionally or by
    # keyword, keeps working unchanged.
    assert SettlementStep(0, "researcher", None, 0.05, True).output_summary is None


def test_the_bound_is_one_line_a_buyer_reads() -> None:
    assert 180 <= OUTPUT_SUMMARY_MAX_CHARS <= 500
