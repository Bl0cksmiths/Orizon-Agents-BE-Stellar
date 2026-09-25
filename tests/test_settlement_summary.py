"""The per-step output summary a settlement keeps (story 4.05).

A dispute is about what a step produced, and the trace that showed it does not
survive the window — so the settlement carries the line. Pinned here: it
round-trips through the one JSON column, and a row written before the field
existed still reads, because those settlements are still inside their windows.
"""

from __future__ import annotations

import json

import pytest

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


# ── what the JSON column will actually take ───────────────────────────────


def test_text_postgres_would_refuse_is_cleaned_rather_than_costing_the_window() -> None:
    """`json.dumps` emits a NUL and a lone surrogate without complaint and
    Python reads them back, so a round trip proves nothing — `jsonb` refuses
    both, the INSERT raises inside a best-effort try, and the settlement is
    never written. The buyer has paid by then and has no dispute window at all,
    which is a far worse outcome than an agent name missing a character nobody
    chose.

    `agent_id` and `agent_name` come off a plan unsanitised. `output_summary`
    has been through `sanitize_untrusted`, which strips the NUL with the other
    control characters but leaves surrogates, so it is cleaned here too."""
    steps = (SettlementStep(0, "resea\x00rcher", "Resear\ud800cher", 0.05, True, "12 sou\x00rces\udfff"),)

    raw = steps_to_json(steps)

    # None of the three escapes Postgres would reject is in what is sent.
    assert "\\u0000" not in raw
    assert "\\ud800" not in raw
    assert "\\udfff" not in raw
    # And what is stored is still the record, minus the characters it could
    # never have carried.
    (step,) = steps_from_json(raw)
    assert step.agent_id == "researcher"
    assert step.agent_name == "Researcher"
    assert step.output_summary == "12 sources"
    assert step.price_usdc == 0.05


@pytest.mark.parametrize("price", [float("inf"), float("-inf"), float("nan")], ids=["inf", "-inf", "nan"])
def test_a_non_finite_price_is_refused_by_name_rather_than_by_the_database(price: float) -> None:
    """`PlanStep.est_price_usdc` is validated `ge=0`, and `float("inf") >= 0`
    is True — so an infinite price reaches the serialiser from an ordinary
    plan. It is the number a credit for the step is computed from, so there is
    nothing honest to put in its place: this fails loudly, naming the step, the
    way a settlement that promised a refund of infinity would not."""
    steps = (SettlementStep(0, "researcher", "Researcher", price, True, None),)

    with pytest.raises(ValueError) as refused:
        steps_to_json(steps)

    assert "step 0" in str(refused.value)
    assert "researcher" in str(refused.value)


def test_an_ordinary_breakdown_is_untouched() -> None:
    """The cleaning may not edit a record that needed no cleaning: an agent
    name, an id and a summary come back exactly as they were given."""
    steps = (SettlementStep(0, "researcher", "Researcher — EU", 0.05, True, "12 sources, 3 conflicting"),)

    assert steps_from_json(steps_to_json(steps)) == steps
